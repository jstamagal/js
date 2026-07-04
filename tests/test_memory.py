from __future__ import annotations

import json
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
    assert "skipped 1" in err
    assert str(f) in err


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
