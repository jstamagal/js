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
import time
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
    if not memory_file.exists():
        return []
    messages: list[dict] = []
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
                continue
            if rec.kind != "message" or rec.message is None:
                continue
            if rec.message.get("role") in {"user", "assistant", "tool", "system"}:
                messages.append(rec.message)
    if skipped_versions:
        msgs.warn(msgs.SESSION_RECORDS_SKIPPED, path=memory_file,
                  records=f"{skipped_versions} record{'' if skipped_versions == 1 else 's'}", version=SCHEMA_VERSION)
    return _heal_orphaned_tool_calls(messages)


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


def _message_record(message: dict, stamp: dict | None) -> Record:
    return Record(kind="message", ts=time.time(), message=message,
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
    if not memory_file.exists():
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
