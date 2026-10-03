"""JSONL conversation persistence. Lock-protected, fsync-after-write,
version-tagged. Loader ignores records it doesn't understand.

Every record carries an `id` and a `parent` (`js.session_store.append`); replay
reads the file in order and does not use them. An assistant message record
carries a `stamp`: the model, provider and reasoning level it was written
under. Every write brings the session's `.txt`
transcript up to date (`js.session_text`)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import messages as msgs
from . import session_store
from . import session_text

SCHEMA_VERSION = 1


@dataclass
class Record:
    kind: Literal["message", "mark"]
    ts: float
    version: int = SCHEMA_VERSION
    message: dict | None = None     # set when kind == "message"
    marker: str | None = None       # set when kind == "mark"
    stamp: dict | None = None       # set on an assistant message: model, provider, reasoning

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict) -> Record | None:
        kind = d.get("kind")
        if kind not in {"message", "mark"}:
            return None
        return cls(
            kind=kind,
            ts=float(d.get("ts", time.time())),
            version=SCHEMA_VERSION,
            message=d.get("message"),
            marker=d.get("marker"),
            stamp=d.get("stamp") if isinstance(d.get("stamp"), dict) else None,
        )


def _migrate_record(d: dict) -> dict | None:
    """Accept a raw record dict only if it is on the current SCHEMA_VERSION.

    Placeholder migration hook: a future SCHEMA_VERSION bump should teach this
    function to upgrade an older record's shape instead of discarding it. Until
    then, a version mismatch returns None and the caller counts it — a record
    dropped here is never silent.

    Only records this schema owns are judged. The conversation file also carries
    records from other writers, each versioning itself independently, and their
    counters are not comparable to this one.
    """
    if d.get("kind") not in {"message", "mark"}:
        return None
    if d.get("version") == SCHEMA_VERSION:
        return d
    return None


def _open_locked(path: Path, mode: str):
    """Open with appropriate fcntl lock for the mode. Caller must close."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, mode)
    lock = fcntl.LOCK_EX if "a" in mode or "w" in mode else fcntl.LOCK_SH
    fcntl.flock(f.fileno(), lock)
    return f


def _heal_orphaned_tool_calls(messages: list[dict]) -> list[dict]:
    """Make every assistant ``tool_calls`` message be followed by exactly one tool
    message per ``tool_call_id`` — a hard requirement of OpenAI-shape providers
    (DeepSeek rejects the whole request otherwise). Results lost to crashes or
    early-exit bugs are backfilled with a synthetic error. A second result for an
    id already answered, or a result with no call in front of it, is dropped: a
    cancelled turn can land the real result after the synthetic one, and the SDK
    refuses to send either shape."""
    healed: list[dict] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "tool":
            i += 1
            continue
        healed.append(msg)
        i += 1
        calls = msg.get("tool_calls") if msg.get("role") == "assistant" else None
        if not calls:
            continue
        expected = {call.get("id") for call in calls}
        answered: set[str] = set()
        while i < len(messages) and messages[i].get("role") == "tool":
            cid = messages[i].get("tool_call_id")
            if cid in expected and cid not in answered:
                answered.add(cid)
                healed.append(messages[i])
            i += 1
        for call in calls:
            cid = call.get("id")
            if cid and cid not in answered:
                healed.append({
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": (call.get("function") or {}).get("name", ""),
                    "content": "ERROR: tool result was not recorded (session interrupted)",
                })
    return healed


def balance_orphaned_tool_calls(messages: list[dict]) -> list[dict]:
    """Public entry to the tool-result heal, for the live REPL to repair the
    in-memory history before each turn (the on-load path in `load_messages`
    already heals the persisted copy)."""
    return _heal_orphaned_tool_calls(messages)


# An assistant message's reasoning: the text, and the signed parts with the
# provider and model they came from (`model_client.signed_reasoning_parts`).
_REASONING_KEYS = frozenset({"reasoning_content", "reasoning_parts", "reasoning_from"})
SIGNED_REASONING_KEYS = ("reasoning_parts", "reasoning_from")


def drop_signed_reasoning(messages: list[dict], start: int = 0) -> int:
    """Remove the signed reasoning of every message from ``start`` on, in place.

    A signature (an Anthropic thinking signature, a Codex encrypted item) is
    bound to the history before it, so once js edits an earlier message the
    signed reasoning after that edit cannot be replayed. Each changed message
    is replaced by a copy that keeps ``reasoning_content``. Returns how many
    messages changed.
    """
    changed = 0
    for index in range(max(0, start), len(messages)):
        msg = messages[index]
        if isinstance(msg, dict) and any(key in msg for key in SIGNED_REASONING_KEYS):
            messages[index] = {k: v for k, v in msg.items() if k not in SIGNED_REASONING_KEYS}
            changed += 1
    return changed


def _strip_orphan_reasoning(messages: list[dict]) -> list[dict]:
    """Project history to the tool-call-only reasoning view (`load_messages`)."""
    out: list[dict] = []
    for msg in messages:
        if msg.get("role") == "assistant" and not msg.get("tool_calls") and _REASONING_KEYS & msg.keys():
            cleaned = {k: v for k, v in msg.items() if k not in _REASONING_KEYS}
            out.append(cleaned)
        else:
            out.append(msg)
    return out



def _without_answer_reasoning_text(messages: list[dict]) -> list[dict]:
    """The history as persistence compares it: final answers without
    ``reasoning_content``, so a history whose answers lack the reasoning text
    appends without replacing the journal's. Signed reasoning is compared, so
    a drop of it is written as a rollback."""
    return [
        {k: v for k, v in msg.items() if k != "reasoning_content"}
        if msg.get("role") == "assistant" and not msg.get("tool_calls") and "reasoning_content" in msg
        else msg
        for msg in messages
    ]


def _compaction_summary_message(summary: str) -> dict:
    return {"role": "user", "content": f"<compaction-summary>\n{summary}\n</compaction-summary>"}


# User messages js writes on its own: reminders and compaction summaries.
_HARNESS_USER_PREFIXES = ("<js-reminder>", "<compaction-summary>")


def turn_cut_off(messages: list[dict]) -> bool:
    """Whether the last turn in `messages` ended without a finished reply.

    Trailing user messages js wrote on its own are passed over. The turn was
    cut off when what is left ends on a user or tool message, or on an
    assistant message that carries tool calls or an `incomplete_reason`."""
    index = len(messages) - 1
    while index >= 0:
        message = messages[index]
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str) and content.startswith(_HARNESS_USER_PREFIXES):
            index -= 1
            continue
        break
    if index < 0:
        return False
    last = messages[index]
    role = last.get("role")
    if role == "assistant":
        return bool(last.get("tool_calls") or last.get("incomplete_reason"))
    return role in ("user", "tool")


def _parse_compaction_marker(marker: str) -> dict | None:
    if not marker.startswith("compaction:"):
        return None
    try:
        data = json.loads(marker.split(":", 1)[1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        return None
    return data

def load_replay_messages(memory_file: Path) -> list[dict]:
    """Return the OpenAI-shape message list from disk, honoring control marks.

    This is the history a turn replays to the model: every assistant keeps its
    reasoning, and `model_client.history_to_ai_messages` applies the transport's
    replay policy.
    """
    messages, skipped_versions = _replay(memory_file)
    if skipped_versions:
        msgs.warn(msgs.SESSION_RECORDS_SKIPPED, path=memory_file,
                  records=f"{skipped_versions} record{'' if skipped_versions == 1 else 's'}", version=SCHEMA_VERSION)
    return messages


def _compared(message: dict) -> dict:
    """``message`` as persistence compares it (`_without_answer_reasoning_text`)."""
    return _without_answer_reasoning_text([message])[0]


def _message_key(message: dict) -> str:
    return json.dumps(_compared(message), sort_keys=True, default=str)


def _carries_tool_ids(message: dict) -> bool:
    return bool(_tool_call_ids(message)) or (message.get("role") == "tool" and bool(message.get("tool_call_id")))


class _Reappends:
    """Undoes the runs the session writer appended again before 2026-08-14.

    When the history on disk and the one in memory differed at some message,
    that writer appended the in-memory history from that message on once more,
    without the rollback mark that cuts the disk copy back. Such a file repeats
    its own history: a run of records each equal to the next message of a
    stretch already replayed, then the new turn. A run is dropped, keeping the
    stretch it copies, once it has copied the whole tail of the history, or
    when it stops short of that but copied a tool call or result, whose ids
    are unique. That leaves the history the writer held in memory, so a later
    mark counts in the list it was written against. Replay uses this only for
    a file that repeats a tool-call id, which no provider takes."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self.reset()

    def reset(self) -> None:
        """Index the history as it stands, after a mark rewrote it."""
        self.at: dict[str, list[int]] = {}
        self._index(range(len(self.messages)))
        # A run in progress: it began at `base`, copying from each of `starts`;
        # `length` of its records have matched so far, `tools` says whether
        # one of them carried a tool id.
        self.base = 0
        self.starts: list[int] = []
        self.length = 0
        self.tools = False

    def _index(self, positions: range) -> None:
        for position in positions:
            self.at.setdefault(_message_key(self.messages[position]), []).append(position)

    def finish(self) -> None:
        """End a run in progress: before a mark, and at the end of the file."""
        if not self.starts:
            return
        if self.tools:
            del self.messages[self.base:]
        else:
            self._index(range(self.base, len(self.messages)))
        self.starts = []

    def append(self, message: dict) -> None:
        messages = self.messages
        if self.starts:
            compared = _compared(message)
            alive = [start for start in self.starts if _compared(messages[start + self.length]) == compared]
            if alive:
                messages.append(message)
                self.length += 1
                self.tools = self.tools or _carries_tool_ids(message)
                if any(start + self.length == self.base for start in alive):
                    del messages[self.base:]
                    self.starts = []
                else:
                    self.starts = alive
                return
            self.finish()
        starts = list(self.at.get(_message_key(message), ()))
        self.base = len(messages)
        messages.append(message)
        if not starts:
            self._index(range(self.base, self.base + 1))
        elif self.base - 1 in starts:
            del messages[self.base:]
        else:
            self.starts, self.length, self.tools = starts, 1, _carries_tool_ids(message)


def _tool_call_ids(message: dict) -> list[str]:
    if message.get("role") != "assistant":
        return []
    return [call["id"] for call in message.get("tool_calls") or () if isinstance(call, dict) and call.get("id")]


def _replay(memory_file: Path, stamps: dict[int, tuple[dict, dict]] | None = None) -> tuple[list[dict], int]:
    """The replayed history and the number of records skipped for their version.
    ``stamps`` collects ``id(message) -> (message, stamp)`` for every stamped reply read.
    A file whose message records repeat a tool-call id is read again undoing
    the runs the old writer appended twice (`_Reappends`)."""
    messages, skipped_versions, repeats = _replay_records(memory_file, stamps, undo_reappends=False)
    if repeats:
        if stamps is not None:
            stamps.clear()
        messages, skipped_versions, _repeats = _replay_records(memory_file, stamps, undo_reappends=True)
    return messages, skipped_versions


def _replay_records(memory_file: Path, stamps: dict[int, tuple[dict, dict]] | None, *,
                    undo_reappends: bool) -> tuple[list[dict], int, bool]:
    """The replayed history, the records skipped for their version, and
    whether two message records carry the same tool-call id."""
    if not memory_file.exists():
        return [], 0, False
    messages: list[dict] = []
    undo = _Reappends(messages) if undo_reappends else None
    call_ids: set[str] = set()
    repeats = False
    skipped_versions = 0
    with _open_locked(memory_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            migrated = _migrate_record(raw)
            if migrated is None:
                if (
                    isinstance(raw, dict)
                    and "version" in raw
                    and raw.get("kind") in {"message", "mark"}
                ):
                    skipped_versions += 1
                continue
            rec = Record.from_dict(migrated)
            if rec is None:
                continue
            if rec.kind == "mark":
                if undo is not None:
                    undo.finish()
                if rec.marker == "session_reset":
                    messages.clear()
                elif rec.marker and rec.marker.startswith("rollback_to:"):
                    try:
                        keep = max(0, int(rec.marker.split(":", 1)[1]))
                    except ValueError:
                        continue
                    # The index rides in POST-heal (live) space — the caller cut
                    # `state["messages"][:keep]` against a healed list — so heal
                    # first, else a synthetic tool result inserted on reload shifts
                    # every later offset and the mark truncates the wrong message.
                    messages[:] = _heal_orphaned_tool_calls(messages)
                    del messages[keep:]
                elif rec.marker:
                    data = _parse_compaction_marker(rec.marker)
                    if data is not None:
                        messages[:] = _heal_orphaned_tool_calls(messages)
                        keep_from = int(data.get("keep_from", len(messages)))
                        keep_from = max(0, min(keep_from, len(messages)))
                        rehydrated = data.get("rehydrated")
                        tail = messages[keep_from:]
                        drop_signed_reasoning(tail)
                        messages[:] = [_compaction_summary_message(data["summary"]),
                                       *([rehydrated] if rehydrated else []), *tail]
                if undo is not None:
                    undo.reset()
                continue
            if rec.kind != "message" or rec.message is None:
                continue
            if rec.message.get("role") in {"user", "assistant", "tool", "system"}:
                for call_id in _tool_call_ids(rec.message):
                    repeats = repeats or call_id in call_ids
                    call_ids.add(call_id)
                if undo is not None:
                    undo.append(rec.message)
                else:
                    messages.append(rec.message)
                stamp = _reply_stamp(rec.message, rec.stamp)
                if stamps is not None and stamp is not None:
                    stamps[id(rec.message)] = (rec.message, stamp)
    if undo is not None:
        undo.finish()
    return _heal_orphaned_tool_calls(messages), skipped_versions, repeats


def load_messages(memory_file: Path, *, preserve_reasoning: bool = False) -> list[dict]:
    """The `load_replay_messages` history. ``preserve_reasoning=True`` returns it
    unchanged; the default projects it to tool-call reasoning only."""
    messages = load_replay_messages(memory_file)
    return messages if preserve_reasoning else _strip_orphan_reasoning(messages)


def _append(memory_file: Path, rec: Record, *, refresh: bool = True) -> None:
    session_store.append(memory_file, rec.as_dict())
    if refresh:
        session_text.refresh(memory_file)


def stamp_for(model: str | None, provider: str | None, reasoning: str | None) -> dict:
    """The stamp an assistant message is written under."""
    return {"model": model, "provider": provider, "reasoning": reasoning}


# When each live message happened, by message object. A turn's messages are
# persisted when the turn ends; the record written for a noted message carries
# the time noted, not the write time. An entry leaves when its record is
# written, or when more than _EVENT_TIMES_MAX are held.
_EVENT_TIMES: OrderedDict[int, tuple[dict, float]] = OrderedDict()
_EVENT_TIMES_MAX = 4096
_event_times_lock = threading.Lock()


def note_time(message: dict, ts: float | None = None) -> dict:
    """Remember when ``message`` happened (now when ``ts`` is None); the record
    later written for it carries that time. Returns ``message``."""
    with _event_times_lock:
        _EVENT_TIMES[id(message)] = (message, time.time() if ts is None else ts)
        _EVENT_TIMES.move_to_end(id(message))
        while len(_EVENT_TIMES) > _EVENT_TIMES_MAX:
            _EVENT_TIMES.popitem(last=False)
    return message


def _take_time(message: dict) -> float:
    """The noted time of ``message``, else now. The note is dropped."""
    with _event_times_lock:
        entry = _EVENT_TIMES.get(id(message))
        if entry is None or entry[0] is not message:
            return time.time()
        del _EVENT_TIMES[id(message)]
        return entry[1]


def _message_record(message: dict, stamp: dict | None) -> Record:
    return Record(kind="message", ts=_take_time(message), message=message,
                  stamp=stamp if stamp is not None and message.get("role") == "assistant" else None)


def append_message(memory_file: Path, message: dict, stamp: dict | None = None) -> None:
    _append(memory_file, _message_record(message, stamp))


def persist_messages(memory_file: Path, messages: list[dict], stamp: dict | None = None) -> None:
    """Append the live suffix, retaining replaced records in the journal.
    Each appended assistant message carries `stamp`."""
    persisted = _without_answer_reasoning_text(load_replay_messages(memory_file))
    comparable = _without_answer_reasoning_text(messages)
    common = 0
    for old, new in zip(persisted, comparable):
        if old != new:
            break
        common += 1
    if common < len(persisted):
        _append(memory_file, Record(kind="mark", ts=time.time(), marker=f"rollback_to:{common}"), refresh=False)
    for message in messages[common:]:
        _append(memory_file, _message_record(message, stamp), refresh=False)
    if common < len(persisted) or messages[common:]:
        session_text.refresh(memory_file)


def append_mark(memory_file: Path, marker: str) -> None:
    _append(memory_file, Record(kind="mark", ts=time.time(), marker=marker))


_SYSTEM_MARK = "system:"


def append_system_prompt(memory_file: Path, system: str) -> None:
    """Record the system prompt a session was started with."""
    append_mark(memory_file, _SYSTEM_MARK + json.dumps({"system": system}, separators=(",", ":")))


def _last_mark_payload(memory_file: Path, prefix: str) -> str | None:
    """The text after `prefix` in the newest mark that starts with it, or None."""
    try:
        with _open_locked(memory_file, "r") as stream:
            lines = stream.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        marker = record.get("marker") if isinstance(record, dict) else None
        if isinstance(marker, str) and marker.startswith(prefix):
            return marker[len(prefix):]
    return None


def _reply_stamp(message: object, stamp: object) -> dict | None:
    """``stamp`` when it is a usable stamp on an assistant message, else None."""
    if (isinstance(stamp, dict) and isinstance(message, dict) and message.get("role") == "assistant"
            and isinstance(stamp.get("model"), str) and stamp["model"]):
        return stamp
    return None


def _rewrites_history(marker: object) -> bool:
    """Whether a mark can drop messages written before it from the replayed history."""
    return isinstance(marker, str) and (
        marker == "session_reset" or marker.startswith("rollback_to:") or marker.startswith("compaction:"))


def _lines_newest_first(stream, block: int = 1 << 16):
    """The lines of a binary ``stream``, last line first, read back from the end in blocks."""
    stream.seek(0, 2)
    position = stream.tell()
    rest = b""
    while position > 0:
        step = min(block, position)
        position -= step
        stream.seek(position)
        lines = (stream.read(step) + rest).split(b"\n")
        rest = lines.pop(0)
        yield from reversed(lines)
    if rest:
        yield rest


def last_reply_stamp(memory_file: Path) -> dict | None:
    """The stamp of the newest assistant message in the replayed history that has one, or None.

    The file is read back from its end. Reaching a mark that rewrites history
    before a stamped reply sends the lookup through a full replay, so a reply
    that a rollback, reset or compaction dropped does not count."""
    try:
        with _open_locked(memory_file, "rb") as stream:
            for line in _lines_newest_first(stream):
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict) or _migrate_record(record) is None:
                    continue
                if record["kind"] == "mark":
                    if _rewrites_history(record.get("marker")):
                        break
                    continue
                stamp = _reply_stamp(record.get("message"), record.get("stamp"))
                if stamp is not None:
                    return stamp
            else:
                return None
    except OSError:
        return None
    stamps: dict[int, tuple[dict, dict]] = {}
    messages, _skipped = _replay(memory_file, stamps)
    for message in reversed(messages):
        entry = stamps.get(id(message))
        if entry is not None and entry[0] is message:
            return entry[1]
    return None


def load_system_prompt(memory_file: Path) -> str | None:
    """The system prompt a session was started with, or None when unrecorded.

    A resumed session has to send the bytes it sent before. Rebuilding the prompt
    puts a fresh clock, uptime and load average in front of an append-only
    history, so the request no longer shares a prefix with the one that built the
    conversation and every previously cached token is re-read at full price."""
    raw = _last_mark_payload(memory_file, _SYSTEM_MARK)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    system = payload.get("system") if isinstance(payload, dict) else None
    return system if isinstance(system, str) and system else None


_PROMPT_SEEN_MARK = "prompt_seen:"


def prompt_fingerprint(system: str) -> str:
    return hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]


def last_prompt_seen(memory_file: Path) -> str | None:
    """Fingerprint of the on-disk prompt at the newest launch, or None when no
    launch recorded one."""
    seen = _last_mark_payload(memory_file, _PROMPT_SEEN_MARK)
    return seen or None


def record_prompt_seen(memory_file: Path, prompt: str) -> bool:
    """Append this launch's `prompt_seen:` mark and report whether the on-disk
    prompt differs from the one the previous launch saw.

    A launch with no earlier mark reports no change: at birth the recorded
    prompt is the on-disk prompt."""
    previous = last_prompt_seen(memory_file)
    current = prompt_fingerprint(prompt)
    append_mark(memory_file, _PROMPT_SEEN_MARK + current)
    return previous is not None and previous != current


_TURN_MODE_MARK = "turn_mode:"


def record_turn_mode(memory_file: Path, mode: str) -> str | None:
    """Note that a turn runs in ``mode`` and return the mode the previous turn
    ran in when it was a different one, else None.

    A `turn_mode:` mark is appended only when the mode changes, so the newest
    one is always the mode of the last turn. A session with no mark yet reports
    no change."""
    previous = _last_mark_payload(memory_file, _TURN_MODE_MARK) or None
    if previous == mode:
        return None
    append_mark(memory_file, _TURN_MODE_MARK + mode)
    return previous


def first_turn_mode(memory_file: Path) -> str | None:
    """The mode the session's first turn ran in: the oldest `turn_mode:` mark,
    or None when no turn has recorded one."""
    try:
        with _open_locked(memory_file, "r") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                marker = record.get("marker") if isinstance(record, dict) else None
                if isinstance(marker, str) and marker.startswith(_TURN_MODE_MARK):
                    return marker[len(_TURN_MODE_MARK):] or None
    except OSError:
        return None
    return None


_WORKSPACE_MARK = "workspace:"


def append_workspace_mark(memory_file: Path, *, root: str | None, cwd: str, binds: list[str]) -> None:
    """Record where the session works after a /cd, /add or /drop: the -C root
    it ran under (None without one), its working directory, and the /add binds."""
    payload = {"root": root, "cwd": cwd, "binds": binds}
    append_mark(memory_file, _WORKSPACE_MARK + json.dumps(payload, separators=(",", ":")))


def last_workspace(memory_file: Path) -> dict | None:
    """The newest `workspace:` mark's payload, or None."""
    raw = _last_mark_payload(memory_file, _WORKSPACE_MARK)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def append_compaction_mark(memory_file: Path, *, summary: str, keep_from: int, forced: bool = False,
                           trigger: dict | None = None, rehydrated: dict | None = None) -> None:
    payload = {"summary": summary, "keep_from": int(keep_from), "forced": bool(forced)}
    if rehydrated is not None:
        payload["rehydrated"] = rehydrated
    if trigger is not None:
        payload["trigger"] = trigger
    append_mark(memory_file, "compaction:" + json.dumps(payload, separators=(",", ":")))


def wipe(memory_file: Path) -> Path | None:
    """Rotate the memory file to a .bak suffix. Returns the .bak path or None."""
    if memory_file == Path(os.devnull) or not memory_file.exists():
        return None
    bak = memory_file.with_suffix(memory_file.suffix + ".bak")
    if bak.exists():
        idx = 1
        while True:
            candidate = memory_file.with_suffix(memory_file.suffix + f".bak.{idx}")
            if not candidate.exists():
                bak = candidate
                break
            idx += 1
    memory_file.rename(bak)
    session_text.forget(memory_file)
    return bak


def append_tool_surface(memory_file: Path, state: dict) -> None:
    """Persist session tool visibility independently of compacted messages."""
    append_mark(memory_file, 'tool_surface:' + json.dumps(state, separators=(',', ':')))


def load_tool_surface(memory_file: Path) -> dict | None:
    """Read the latest surface snapshot after the last session reset."""
    try:
        with _open_locked(memory_file, 'r') as stream:
            lines = stream.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get('kind') != 'mark' or record.get('version') != SCHEMA_VERSION:
            continue
        marker = record.get('marker')
        if marker == 'session_reset':
            return None
        if not isinstance(marker, str) or not marker.startswith('tool_surface:'):
            continue
        try:
            state = json.loads(marker[len('tool_surface:'):])
        except json.JSONDecodeError:
            continue
        if isinstance(state, dict) and state.get('version') == 1:
            return state
    return None
