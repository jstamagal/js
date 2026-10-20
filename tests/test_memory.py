from __future__ import annotations

import json
import re
from pathlib import Path

from js import memory as M


def _assistant_toolcall(cid: str = "tc1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": cid, "type": "function", "function": {"name": "read", "arguments": "{}"}}],
    }


def test_rollback_index_is_applied_in_post_heal_space(tmp_path: Path):
    """An interrupted turn leaves an orphan tool_call on disk; the next turn's
    ``rollback_to`` index was computed against the HEALED (live) list. On reload the
    healer inserts a synthetic tool result, so the index must be applied post-heal —
    otherwise the aborted prompt leaks back into the conversation."""
    f = tmp_path / "s.jsonl"
    M.append_message(f, {"role": "user", "content": "u1"})
    M.append_message(f, _assistant_toolcall())          # orphan: no tool result recorded
    M.append_mark(f, "turn_interrupted")
    M.append_message(f, {"role": "user", "content": "aborted-prompt"})
    M.append_mark(f, "rollback_to:3")                   # 3 = len of healed list before u2

    out = M.load_messages(f)

    assert [m.get("content") for m in out if m.get("role") == "user"] == ["u1"]
    assert not any(m.get("content") == "aborted-prompt" for m in out)
    assert any(m.get("role") == "tool" for m in out)    # synthetic result survived


def _tool_result(cid: str, text: str) -> dict:
    return {"role": "tool", "tool_call_id": cid, "content": text}


def _reappending_writer(f: Path) -> list[dict]:
    """The session file the writer left before 2026-08-14: each save appended
    the live history after its first message again, with no rollback mark,
    then the new turn. Returns the live history it held at the end."""
    u0, a1 = {"role": "user", "content": "yo"}, {"role": "assistant", "content": "yo KING"}
    u2 = {"role": "user", "content": "read it"}
    a3, t4 = _assistant_toolcall("call_X"), _tool_result("call_X", "file text")
    a5, u6 = {"role": "assistant", "content": "read it all"}, {"role": "user", "content": "count"}
    a7, t8 = _assistant_toolcall("call_Y"), _tool_result("call_Y", "42")
    a9 = {"role": "assistant", "content": "counted"}
    for message in (u0, a1, u2, a1, u2, a3, t4, a5, u6, a1, u2, a3, t4, a5, u6, a7, t8, a9):
        M.append_message(f, message)
    return [u0, a1, u2, a3, t4, a5, u6, a7, t8, a9]


def test_history_the_old_writer_appended_again_replays_once(tmp_path: Path):
    f = tmp_path / "s.jsonl"
    live = _reappending_writer(f)

    assert M.load_replay_messages(f) == live


def test_a_rollback_after_the_appended_runs_counts_in_the_live_history(tmp_path: Path):
    f = tmp_path / "s.jsonl"
    live = _reappending_writer(f)
    M.append_mark(f, "rollback_to:6")
    M.append_message(f, {"role": "user", "content": "again"})

    assert M.load_replay_messages(f) == [*live[:6], {"role": "user", "content": "again"}]


def test_a_provider_reusing_a_tool_call_id_keeps_every_message(tmp_path: Path):
    f = tmp_path / "s.jsonl"
    written = [
        {"role": "user", "content": "one"}, _assistant_toolcall("call_0"), _tool_result("call_0", "a"),
        {"role": "assistant", "content": "first"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_0", "type": "function", "function": {"name": "read", "arguments": '{"path": "b"}'}}]},
        _tool_result("call_0", "b"), {"role": "assistant", "content": "second"},
    ]
    for message in written:
        M.append_message(f, message)

    assert M.load_replay_messages(f) == written


def test_a_repeated_exchange_in_a_sendable_history_is_kept(tmp_path: Path):
    f = tmp_path / "s.jsonl"
    written = [{"role": "user", "content": "APE"}, {"role": "assistant", "content": "*grunt*"}] * 2
    for message in written:
        M.append_message(f, message)

    assert M.load_replay_messages(f) == written


def test_version_mismatch_warns_instead_of_silently_dropping_history(tmp_path: Path, capsys):
    """Bumping SCHEMA_VERSION must never make old sessions load as empty with no
    signal — js never crashes on bad input, but it must also never go silent."""
    f = tmp_path / "s.jsonl"
    M.append_message(f, {"role": "user", "content": "kept"})
    with f.open("a") as fh:
        fh.write(json.dumps({"kind": "message", "ts": 0.0, "version": 999, "message": {"role": "user", "content": "old"}}) + "\n")

    out = M.load_messages(f)

    assert [m.get("content") for m in out] == ["kept"]   # current-version record survives
    err = capsys.readouterr().err
    assert str(f) in err
    assert re.search(r"(?<!\d)1(?!\d)", err.replace(str(f), ""))  # the count of skipped records


def test_compaction_keep_from_is_applied_in_post_heal_space(tmp_path: Path):
    """keep_from is computed against the healed list too; on reload the tail must be
    retained from the same message the caller intended, not shifted by the synthetic
    heal insertion."""
    f = tmp_path / "s.jsonl"
    M.append_message(f, {"role": "user", "content": "u1"})
    M.append_message(f, _assistant_toolcall())          # orphan -> heals to +1 message
    M.append_mark(f, "turn_interrupted")
    M.append_message(f, {"role": "user", "content": "keep-user"})
    M.append_message(f, {"role": "assistant", "content": "keep-assistant"})
    # healed live space: [u1, assistant(tc), synthetic-tool, keep-user, keep-assistant]
    M.append_compaction_mark(f, summary="S", keep_from=3)

    out = M.load_messages(f)

    assert out[0]["content"] == "<compaction-summary>\nS\n</compaction-summary>"
    assert [m.get("content") for m in out[1:]] == ["keep-user", "keep-assistant"]


def test_heal_drops_duplicate_and_orphaned_tool_results():
    from ai.providers import history_utils

    from js import model_client

    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "shell", "content": "one"},
        {"role": "tool", "tool_call_id": "c2", "name": "shell", "content": "ERROR: tool result was not recorded (session interrupted)"},
        {"role": "tool", "tool_call_id": "c2", "name": "shell", "content": "two (landed late)"},
        {"role": "tool", "tool_call_id": "never-called", "name": "shell", "content": "stray"},
        {"role": "user", "content": "again"},
        {"role": "tool", "tool_call_id": "c1", "name": "shell", "content": "stray after user"},
    ]

    healed = M.balance_orphaned_tool_calls(messages)

    assert [m.get("tool_call_id") for m in healed if m["role"] == "tool"] == ["c1", "c2"]
    assert healed[-1] == {"role": "user", "content": "again"}
    assert history_utils.check_tool_ids(model_client.history_to_ai_messages("", healed)) == []


def test_replay_of_a_compacted_journal_does_not_hold_the_history_compaction_dropped(tmp_path: Path):
    """Only a caller that asks for the retained history pays for it."""
    import tracemalloc

    f = tmp_path / "s.jsonl"
    body = "x" * 10_000
    _write_records(f, [record for n in range(200) for record in (
        _message_record({"role": "user", "content": f"{n} {body}"}),
        {"kind": "mark", "version": M.SCHEMA_VERSION, "ts": 1.0,
         "marker": "compaction:" + json.dumps({"summary": "s", "keep_from": 99})},
    )])

    tracemalloc.start()
    try:
        M.load_replay_messages(f)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 1_000_000


def _message_record(message: dict) -> dict:
    return {"kind": "message", "version": M.SCHEMA_VERSION, "ts": 1.0, "message": message}


def _write_records(f: Path, records: list[dict]) -> None:
    """Records written straight to the file: a big journal without a
    transcript refresh per record."""
    f.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _old_writer_journal(f: Path, turns: int) -> None:
    """`turns` tool-calling turns, then all of them appended once more, as
    the writer before 2026-08-14 did."""
    history = []
    for n in range(turns):
        history += [{"role": "user", "content": f"u{n}"}, _assistant_toolcall(f"call_{n}"),
                    _tool_result(f"call_{n}", f"r{n}"), {"role": "assistant", "content": f"a{n}"}]
    _write_records(f, [_message_record(message) for message in history + history[1:]])


def test_the_retained_history_of_an_appended_twice_journal_is_read_in_linear_time(tmp_path: Path):
    import time

    def best(f: Path) -> float:
        times = []
        for _ in range(3):
            start = time.perf_counter()
            M.retained_messages(f)
            times.append(time.perf_counter() - start)
        return min(times)

    small, large = tmp_path / "small.jsonl", tmp_path / "large.jsonl"
    _old_writer_journal(small, 250)
    _old_writer_journal(large, 1000)

    assert best(large) < 10 * best(small)


def test_restarting_thinking_keeps_steered_attachments_between_completed_rounds():
    import ai

    from js import model_client

    earlier = {"role": "assistant", "content": "earlier", "reasoning_parts": [
        {"text": "thought", "provider_metadata": {"anthropic": {"signature": "S0"}}}],
        "reasoning_from": {"provider": "anthropic", "model": "m"}}
    prefix = [{"role": "user", "content": "first"}, earlier, {"role": "user", "content": "go"}]
    first = [_assistant_toolcall("c1"), _tool_result("c1", "first result")]
    second = [_assistant_toolcall("c2"), _tool_result("c2", "second result")]
    attachment = ai.types.messages.FilePart(data=b"image", media_type="image/png")
    steered = {"role": "user", "steered": True,
               "content": [ai.types.messages.TextPart(text="use this"), attachment]}
    nudge = {"role": "user", "resume_nudge": True, "content": "continue"}
    messages = [*prefix, *first, steered, *second, nudge]

    assert M.restart_thinking_turn(messages, require_tool_calls=True) == 6

    assert messages[:3] == prefix
    assert messages[4] is steered
    assert messages[6] is nudge
    assert json.loads(messages[3]["content"].split("\n", 1)[1]) == first
    assert json.loads(messages[5]["content"].split("\n", 1)[1]) == second
    projected = model_client.history_to_ai_messages("", messages, provider_id="anthropic", model_id="m")
    assert projected[4].parts[-1] is attachment
