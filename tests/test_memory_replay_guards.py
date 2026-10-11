"""memory.py replay: malformed records skip, destructive marks with a bad
payload are inert, and the old writer's double appends are undone only in
the epoch that shows them (ALL-CLAIMS.md:290-291). Ported from nyira's
scratch acceptance gate; the source journal is never rewritten."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from js import memory as M


def user(text):
    return {'role': 'user', 'content': text}


def assistant(text):
    return {'role': 'assistant', 'content': text}


def pair(cid):
    return [
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': cid, 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': cid, 'content': 'value'},
    ]


def record(message):
    return {'kind': 'message', 'ts': 1, 'version': 1, 'message': message}


def mark(marker):
    return {'kind': 'mark', 'ts': 1, 'version': 1, 'marker': marker}


class MemoryReplayGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='memory-replay-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.refresh = patch.object(M.session_text, 'refresh', lambda *_: None)
        self.refresh.start()
        self.addCleanup(self.refresh.stop)

    def write(self, rows):
        path = self.root / 'session.jsonl'
        path.write_bytes(b'\n'.join(r if isinstance(r, bytes) else json.dumps(r).encode() for r in rows) + b'\n')
        return path

    def test_modern_writer_rollback_keeps_repeated_exchange(self):
        path = self.root / 'writer.jsonl'
        first = [user('hi'), *pair('call_X'), assistant('answer')]
        edited = [user('hi (edited)'), *first[1:]]
        live = [*edited, user('continue'), assistant('ok'), user('continue'), assistant('ok')]
        for messages in (first, edited, live):
            M.persist_messages(path, messages)
        self.assertEqual(M.load_replay_messages(path), live)

    def test_legacy_repair_does_not_leak_past_rewrite_boundary(self):
        legacy = [user('legacy'), *pair('call_X'), *pair('call_X')]
        repeated = [user('continue'), assistant('ok')] * 2
        boundaries = [
            ('reset', 'session_reset', []),
            ('rollback', 'rollback_to:0', []),
            ('compact', 'compaction:' + json.dumps({'summary': 'S', 'keep_from': 3}),
             [user('<compaction-summary>\nS\n</compaction-summary>')]),
        ]
        for name, marker, prefix in boundaries:
            with self.subTest(boundary=name):
                path = self.write([*(record(m) for m in legacy), mark(marker), *(record(m) for m in repeated)])
                raw = path.read_bytes()
                self.assertEqual(M.load_replay_messages(path), prefix + repeated)
                self.assertEqual(path.read_bytes(), raw)

    def test_legacy_reappended_tail_still_repairs(self):
        u0, a1, u2 = user('yo'), assistant('yo KING'), user('read it')
        a3, t4 = pair('call_X')
        a5, u6 = assistant('read it all'), user('count')
        a7, t8 = pair('call_Y')
        a9 = assistant('counted')
        written = [u0, a1, u2, a1, u2, a3, t4, a5, u6, a1, u2, a3, t4, a5, u6, a7, t8, a9]
        path = self.write(record(m) for m in written)
        self.assertEqual(M.load_replay_messages(path), [u0, a1, u2, a3, t4, a5, u6, a7, t8, a9])

    def test_legacy_duplicate_retained_across_compaction_still_repairs(self):
        a, t = pair('retained_X')
        answer, next_user = assistant('answer'), user('next')
        marker = 'compaction:' + json.dumps({'summary': 'S', 'keep_from': 1})
        rows = [*(record(m) for m in (user('old user'), a, t, answer)), mark(marker),
                *(record(m) for m in (a, t, answer, next_user))]
        path = self.write(rows)
        self.assertEqual(M.load_replay_messages(path),
                         [user('<compaction-summary>\nS\n</compaction-summary>'), a, t, answer, next_user])

    def test_malformed_record_keeps_neighbors_and_source(self):
        corrupt = [None, [], 1,
                   {'kind': 'message', 'ts': None, 'version': 1, 'message': user('bad ts')},
                   {'kind': 'message', 'ts': 1, 'version': 1, 'message': 'str'}, b'\xff']
        for bad in corrupt:
            with self.subTest(bad=bad):
                path = self.write([record(user('before')), bad, record(assistant('after'))])
                raw = path.read_bytes()
                messages = M.load_replay_messages(path)
                self.assertIn(user('before'), messages)
                self.assertIn(assistant('after'), messages)
                self.assertEqual(path.read_bytes(), raw)

    def test_malformed_compaction_is_inert(self):
        for keep in (None, 'abc', [], {}, float('nan'), float('inf')):
            with self.subTest(keep=keep):
                marker = 'compaction:' + json.dumps({'summary': 'invalid marker', 'keep_from': keep})
                path = self.write([record(user('before')), mark(marker), record(assistant('after'))])
                self.assertEqual(M.load_replay_messages(path), [user('before'), assistant('after')])

    def test_bad_utf8_does_not_poison_startup_metadata_readers(self):
        surface = {'version': 1, 'loaded': ['read']}
        workspace = {'root': None, 'cwd': '/scratch', 'binds': []}
        path = self.write([
            mark('system:' + json.dumps({'system': 'saved system'})),
            mark('turn_mode:one-shot'), mark('prompt_seen:abc123'),
            mark('workspace:' + json.dumps(workspace)),
            mark('tool_surface:' + json.dumps(surface)), b'\xff',
        ])
        checks = [(M.load_system_prompt, 'saved system'), (M.first_turn_mode, 'one-shot'),
                  (M.last_prompt_seen, 'abc123'), (M.last_workspace, workspace),
                  (M.load_tool_surface, surface)]
        for reader, expected in checks:
            with self.subTest(reader=reader.__name__):
                self.assertEqual(reader(path), expected)

    def test_bad_tool_call_shapes_do_not_crash_the_heal(self):
        bad = [{'role': 'assistant', 'content': '', 'tool_calls': [['x'], {'id': ['y']}, 'z']},
               {'role': 'assistant', 'content': '', 'tool_calls': 'oops'}]
        path = self.write([record(user('hi')), *(record(m) for m in bad), record(assistant('ok'))])
        messages = M.load_replay_messages(path)
        self.assertEqual(messages[0], user('hi'))
        self.assertEqual(messages[-1], assistant('ok'))

    def test_bad_ts_and_kind_types_are_skipped_or_defaulted(self):
        path = self.write([{'kind': ['message'], 'ts': 1, 'version': 1, 'message': user('x')},
                           {'kind': 'message', 'ts': float('nan'), 'version': 1, 'message': user('nan')},
                           {'kind': 'message', 'ts': True, 'version': 1, 'message': user('bool')}])
        self.assertEqual(M.load_replay_messages(path), [user('nan'), user('bool')])

    def test_keep_from_past_the_raw_length_still_counts_in_healed_space(self):
        # Raw history has 2 messages; healing adds a synthetic result, so
        # keep_from=3 keeps nothing of the old history but must not be
        # clamped to the raw length 2 (which would keep the synthetic row).
        a, _t = pair('orphan')
        marker = 'compaction:' + json.dumps({'summary': 'S', 'keep_from': 3})
        path = self.write([record(user('u')), record(a), mark(marker), record(user('next'))])
        self.assertEqual(M.load_replay_messages(path),
                         [user('<compaction-summary>\nS\n</compaction-summary>'), user('next')])

    def test_a_huge_int_ts_does_not_raise(self):
        path = self.write([{'kind': 'message', 'ts': 10 ** 400, 'version': 1, 'message': user('big')}])
        self.assertEqual(M.load_replay_messages(path), [user('big')])
