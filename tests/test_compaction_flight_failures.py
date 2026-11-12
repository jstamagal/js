"""Optional compaction diagnostics cannot veto durable history rewrites."""

import asyncio
import errno
import json
from pathlib import Path

import pytest

from js import compaction, compaction_flight
from test_compaction_flight import _flight_cfg
from test_config_compaction_layers import _compact_test_cfg
from test_turn_budget import _Setup, _tool_history


def no_space(*args, **kwargs):
    raise OSError(errno.ENOSPC, "full diagnostic disk")


def flight(tmp_path, **kwargs):
    return compaction_flight.CompactionFlight(
        _flight_cfg(tmp_path, tmp_path / "session.jsonl"), "system", [],
        trigger={}, forced=True, focus=None, preserve_from=0, details={},
        directory=tmp_path / "flights", **kwargs)


@pytest.mark.parametrize("stage", ["mkdir", "open", "source_hash"])
def test_flight_start_io_failure_is_optional(tmp_path, monkeypatch, stage):
    if stage == "mkdir":
        monkeypatch.setattr(Path, "mkdir", no_space)
    elif stage == "open":
        monkeypatch.setattr(compaction_flight.os, "open", no_space)
    else:
        monkeypatch.setattr(Path, "read_bytes", no_space)
    notices = []
    attempt = flight(tmp_path, emit=lambda *a, **kw: notices.append((a, kw)))
    assert attempt.stream is None
    attempt.record("decision")
    attempt.finish("success", "system", [])
    attempt.close()
    assert len([a for a, _ in notices if a[0] == "compaction_output"]) == 1


def test_warning_io_failure_does_not_replace_optional_log_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "mkdir", no_space)
    attempt = flight(tmp_path, emit=no_space)
    assert attempt.stream is None
    attempt.close()


@pytest.mark.parametrize("stage", ["write", "flush", "fsync", "close"])
def test_flight_late_io_failure_disables_diagnostics_once(tmp_path, monkeypatch, stage):
    notices = []
    attempt = flight(tmp_path, emit=lambda *a, **kw: notices.append((a, kw)))
    original = attempt.stream

    class FaultyStream:
        def __getattr__(self, name):
            return no_space if name == stage else getattr(original, name)

    attempt.stream = FaultyStream()
    if stage == "fsync":
        monkeypatch.setattr(compaction_flight.os, "fsync", no_space)
    if stage == "close":
        attempt.close()
    elif stage == "flush":
        attempt.flush()
    else:
        attempt.finish("success", "system", [])
    attempt.close()
    original.close()
    assert attempt.stream is None
    assert len([a for a, _ in notices if a[0] == "compaction_output"]) == 1


def test_unwritable_flight_does_not_disable_overflow_clearing(tmp_path, monkeypatch):
    setup = _Setup(tmp_path, _tool_history(22))
    bad_dir = tmp_path / "not-a-directory"
    bad_dir.write_text("occupied")
    setup.cfg.settings["COMPACTION_LOG_DIR"] = str(bad_dir)
    assert asyncio.run(setup.budget.recover_overflow(RuntimeError("context overflow"), overflow_round=1, tools=[], specs=[]))
    assert compaction.MICROCOMPACT_CLEARED_MESSAGE in str(setup.convo.ai)
    assert setup.context.compacted_during_turn


@pytest.mark.parametrize("stage", ["after_fsync", "close"])
def test_committed_summary_is_applied_despite_final_flight_failure(tmp_path, monkeypatch, stage):
    messages = [{"role": "user", "content": "old " * 1000},
                {"role": "user", "content": "new"}]
    setup = _Setup(tmp_path, messages, compact={"COMPACTION_KEEP_RECENT_TOKENS": 1})
    calls = []
    original_record = compaction_flight.CompactionFlight._record
    original_close = compaction_flight.CompactionFlight.close

    async def summarize(*a, **kw):
        calls.append(True)
        return "summary"

    def record(self, event, **payload):
        if event == "after" and stage == "after_fsync":
            raise OSError(errno.ENOSPC, "flight after snapshot full")
        return original_record(self, event, **payload)

    def close(self):
        if stage == "close" and self.stream is not None:
            original = self.stream

            class BadClose:
                def close(self):
                    original.close()
                    no_space()

            self.stream = BadClose()
        return original_close(self)

    monkeypatch.setattr(compaction, "summarize", summarize)
    monkeypatch.setattr(compaction_flight.CompactionFlight, "_record", record)
    monkeypatch.setattr(compaction_flight.CompactionFlight, "close", close)
    assert setup.fit(phase="preflight", force=True)
    assert calls == [True]
    assert setup.context.compacted_during_turn
    assert "context_compacted" in setup.kinds()
    assert "context_compaction_failed" not in setup.kinds()
    assert "summary" in str(setup.convo.ai)
    records = [json.loads(line) for line in setup.cfg.session_file.read_text().splitlines()]
    assert sum(str(record.get("marker", "")).startswith("compaction:") for record in records) == 1


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(2), RuntimeError("programming error")])
def test_flight_programming_and_control_errors_still_propagate(tmp_path, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(compaction_flight.CompactionFlight, "_start", fail)
    with pytest.raises(type(failure)):
        flight(tmp_path)


def test_required_session_write_failure_still_raises(tmp_path, monkeypatch):
    cfg = _compact_test_cfg(tmp_path, {"COMPACTION_KEEP_RECENT_TOKENS": 1})
    messages = [{"role": "user", "content": "old " * 1000}, {"role": "user", "content": "new"}]
    before = list(messages)

    async def summarize(*a, **kw):
        return "summary"

    monkeypatch.setattr(compaction, "summarize", summarize)
    monkeypatch.setattr(compaction.M, "persist_messages", no_space)
    with pytest.raises(OSError):
        asyncio.run(compaction.compact_now(cfg, "system", messages, forced=True, log_dir=tmp_path / "flights"))
    assert messages == before
