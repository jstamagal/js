"""What every session file in `fixtures/session_journal` replays to, and the
bytes of the history-rewriting marks js writes.

Each `.jsonl` there is a session file; `expected.json` holds, per file, the
replayed history (`memory.load_replay_messages`), the messages its transcript
keeps (`memory.retained_messages`) and the stamp of its newest reply
(`memory.last_reply_stamp`). The files cover compaction with and without
reattached files, rollback, reset, orphaned tool calls, epochs written by the
writer that appended runs twice, unreadable records, and combinations."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_config_compaction_layers import _compact_test_cfg

from js import compaction, compaction_summary, memory as M
from js.toolkit import ToolContext

FIXTURES = Path(__file__).parent / "fixtures" / "session_journal"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))


def test_every_fixture_has_an_expectation():
    assert sorted(path.stem for path in FIXTURES.glob("*.jsonl")) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_session_file_replays_as_captured(tmp_path, name):
    path = tmp_path / f"{name}.jsonl"
    shutil.copyfile(FIXTURES / f"{name}.jsonl", path)
    before = path.read_bytes()
    expected = EXPECTED[name]

    assert list(M.load_replay_messages(path)) == expected["replay"]
    assert M.retained_messages(path) == expected["retained"]
    assert M.last_reply_stamp(path) == expected["last_reply_stamp"]
    assert path.read_bytes() == before


def _marks(path: Path) -> list[str]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [record["marker"] for record in records if record.get("kind") == "mark"]


def test_persist_writes_a_rollback_to_the_common_prefix(tmp_path):
    path = tmp_path / "s.jsonl"
    first = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"},
             {"role": "user", "content": "more"}]
    M.persist_messages(path, first)
    M.persist_messages(path, [*first[:2], {"role": "user", "content": "edited"}])

    assert _marks(path) == ["rollback_to:2"]


def test_closing_note_rewrites_the_last_user_message(monkeypatch, tmp_path):
    from repl_driver import run_async

    monkeypatch.chdir(tmp_path)
    cfg = _compact_test_cfg(tmp_path, {})
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "agent.yaml").write_text("tools: []\n", encoding="utf-8")
    (cfg.prompts_dir / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    path = cfg.session_file
    M.persist_messages(path, [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"},
                              {"role": "user", "content": "unanswered"}])

    messages = run_async(monkeypatch, cfg, ["/quit bye"]).messages

    assert [mark for mark in _marks(path) if mark.startswith("rollback_to:")] == ["rollback_to:2"]
    assert list(M.load_replay_messages(path)) == messages
    assert len(messages) == 3 and messages[-1]["role"] == "user"
    question, _, note = messages[-1]["content"].partition("\n\n")
    assert question == "unanswered"
    assert note.startswith("<js-reminder>") and note.endswith("bye</js-reminder>")


@pytest.mark.parametrize("file_read", [False, True])
def test_compaction_writes_its_mark_and_the_live_history_replays(monkeypatch, tmp_path, file_read):
    async def summarize(*_args, **_kwargs):
        return "SUMMARY"

    monkeypatch.setattr(compaction_summary, "summarize", summarize)
    # A file read before compaction is re-attached after the summary.
    context = ToolContext()
    if file_read:
        read = tmp_path / "file.py"
        read.write_text("print('kept')\n", encoding="utf-8")
        context.reads.remember(read, "read")
    cfg = _compact_test_cfg(tmp_path, {"COMPACTION_KEEP_RECENT_TOKENS": 10,
                                       "COMPACTION_MIN_SAVINGS_TOKENS": 1})
    messages = [
        {"role": "user", "content": "old " * 5000},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "body " * 500},
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "tail answer", "reasoning_content": "why",
         "reasoning_parts": [{"type": "thinking", "signature": "sig"}],
         "reasoning_from": {"provider": "anthropic", "model": "m"}},
    ]

    result = compaction.compact_now_sync(cfg, "SYSTEM", messages, forced=True, context=context)

    assert compaction.compacted(result)
    marker = _marks(cfg.session_file)[-1]
    trigger = json.loads(marker.split(":", 1)[1])["trigger"]
    rehydrated = json.loads(marker.split(":", 1)[1]).get("rehydrated")
    if file_read:
        assert rehydrated["role"] == "user"
        assert str(read) in rehydrated["content"] and "print('kept')" in rehydrated["content"]
    else:
        assert rehydrated is None
    payload = {"summary": "SUMMARY", "keep_from": 4, "forced": True}
    if rehydrated is not None:
        payload["rehydrated"] = rehydrated
    payload["trigger"] = trigger
    assert marker == "compaction:" + json.dumps(payload, separators=(",", ":"))
    assert sorted(trigger) == ["attempt_id", "phase"] and trigger["phase"] == "manual"
    assert messages == [
        {"role": "user", "content": "<compaction-summary>\nSUMMARY\n</compaction-summary>"},
        *([rehydrated] if rehydrated is not None else []),
        {"role": "assistant", "content": "tail answer", "reasoning_content": "why"},
    ]
    assert list(M.load_replay_messages(cfg.session_file)) == messages
