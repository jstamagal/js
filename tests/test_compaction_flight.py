from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_cli_prompt_mode import _auto_state
from test_config_compaction_layers import _compact_test_cfg

from js import cli, compaction, runtime


def read_flight(cfg):
    files = list(Path(cfg.settings["COMPACTION_LOG_DIR"]).glob("*.jsonl"))
    assert len(files) == 1
    return files[0], [json.loads(line) for line in files[0].read_text().splitlines()]


@pytest.mark.parametrize("failure", [None, RuntimeError("summary failed"), asyncio.CancelledError()])
def test_flight_persists_before_after_and_terminal_outcome(monkeypatch, tmp_path, capsys, failure):
    cfg = replace(_compact_test_cfg(tmp_path, {"COMPACTION_LOG_DIR": str(tmp_path / "flights"),
                                               "COMPACTION_KEEP_RECENT_TOKENS": 1}), paranoid_mode="on")
    cfg.settings["API_KEY"] = "private-key"
    original = [{"role": "user", "content": "old content " * 100}, {"role": "user", "content": "new content"}]
    messages = list(original)

    async def summarize(*a, **kw):
        path, records = read_flight(cfg)
        assert records[1]["messages"] == original
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.name in capsys.readouterr().err
        if failure is not None:
            raise failure
        return "summary"

    monkeypatch.setattr(compaction, "summarize", summarize)
    if failure is None:
        asyncio.run(compaction.compact_now(cfg, "SYSTEM", messages, forced=True, log_dir=compaction.flight_log_dir(cfg), emit=compaction.printing()))
    else:
        with pytest.raises(type(failure)):
            asyncio.run(compaction.compact_now(cfg, "SYSTEM", messages, forced=True, log_dir=compaction.flight_log_dir(cfg), emit=compaction.printing()))
        assert messages == original
    path, records = read_flight(cfg)
    assert records[0]["settings"]["API_KEY"] == "<set>"
    assert records[0]["caller_stack"]
    assert records[1]["system"] == "SYSTEM"
    assert records[-2]["event"] == "after"
    assert records[-2]["messages"] == messages
    expected = "success" if failure is None else "cancelled" if isinstance(failure, asyncio.CancelledError) else "failure"
    assert records[-1]["event"] == expected
    assert records[-1]["id"][:12] in capsys.readouterr().err
    if failure is None:
        marks = [json.loads(line) for line in cfg.session_file.read_text().splitlines()]
        payload = json.loads(next(r["marker"] for r in marks if r.get("marker", "").startswith("compaction:")).split(":", 1)[1])
        assert payload["trigger"]["attempt_id"] == records[0]["id"]
        assert payload["trigger"]["flight_path"] == str(path)


def test_telemetry_writes_autolog_when_optional_debug_log_is_disabled():
    sink = io.StringIO()
    telemetry = runtime.Telemetry(None, trace_sink=sink)
    telemetry.event("context_budget", context_tokens=123, context_window=1000000)
    record = json.loads(sink.getvalue().removeprefix("FLIGHT "))
    assert record["kind"] == "context_budget"
    assert record["context_tokens"] == 123


def test_between_turn_uses_changed_live_window(monkeypatch, tmp_path):
    cfg = _compact_test_cfg(tmp_path, {"CONTEXT_SIZE": 1000000})
    state = _auto_state(cfg)
    state.settings = {"CONTEXT_SIZE": 128000}
    captured = []
    monkeypatch.setattr(compaction, "maybe_auto_compact_async", AsyncMock(side_effect=lambda active, *args, **kwargs: captured.append(active) or compaction.AutoCompactOutcome()))
    cli._maybe_auto_compact(cfg, state)
    assert captured[0].settings["CONTEXT_SIZE"] == 128000
    state.settings["CONTEXT_SIZE"] = 1000000
    cli._maybe_auto_compact(cfg, state)
    assert captured[-1].settings["CONTEXT_SIZE"] == 1000000


def test_same_repl_set_override_updates_anchor_then_request_budget(monkeypatch, tmp_path, capsys):
    from dataclasses import replace

    from repl_driver import output_of, run_async
    from test_lazy_tool_discovery import _result

    from js import settings, supervisor

    monkeypatch.chdir(tmp_path)
    debug_log = tmp_path / "debug.log"
    cfg = _compact_test_cfg(tmp_path, {})
    cfg = replace(cfg, trace=True, max_output_tokens=128, provider_id="openai", debug_log=debug_log,
                  provider_api_key="offline", provider_base_url="http://127.0.0.1:1/v1")
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "agent.yaml").write_text("tools: []\n", encoding="utf-8")
    (cfg.prompts_dir / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    live = settings.seed_defaults()
    live["CONTEXT_SIZE"] = 128000
    live["DEBUG_LOG"] = True
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *args: None)
    answered = asyncio.Event()

    async def sdk(**kwargs):
        answered.set()
        return _result(text="ok")

    monkeypatch.setattr(runtime.model_client, "_stream_async", sdk)
    immediate: list[str] = []

    async def conversation(enter):
        await enter("first")
        await answered.wait()
        [first] = supervisor.get_current().jobs("turn")
        await asyncio.wait({first.task})
        immediate.append(await output_of(enter, capsys, "/set CONTEXT_SIZE 1000000"))
        await enter("second")

    run_async(monkeypatch, cfg, conversation, live_settings=live)

    windows = [json.loads(line)["context_window"] for line in debug_log.read_text(encoding="utf-8").splitlines()
               if json.loads(line)["kind"] == "context_budget"]
    assert windows == [128000, 1000000]
    assert "1000000" in immediate[0]
    assert "1000000" in capsys.readouterr().out


def test_summary_request_and_response_are_recorded(monkeypatch, tmp_path):
    from types import SimpleNamespace
    cfg = _compact_test_cfg(tmp_path, {"COMPACTION_LOG_DIR": str(tmp_path / "flights"), "COMPACTION_KEEP_RECENT_TOKENS": 1})

    async def stream(**kwargs):
        assert kwargs["trace_request"] is True
        kwargs["trace_sink"].write("provider request trace")
        kwargs["on_text"]("summary chunk")
        return SimpleNamespace(text="summary", usage={"input_tokens": 20}, finish_reason="stop")

    monkeypatch.setattr(compaction.model_client, "stream_model_async", stream)
    asyncio.run(compaction.compact_now(cfg, "system", [{"role": "user", "content": "old"},
                                                       {"role": "user", "content": "new"}], forced=True,
                                         log_dir=compaction.flight_log_dir(cfg), emit=compaction.printing()))
    path, records = read_flight(cfg)
    assert any(r["event"] == "summary_trace" and r["text"] == "provider request trace" for r in records)
    assert any(r["event"] == "summary_chunk" and r["text"] == "summary chunk" for r in records)
    response = next(r for r in records if r["event"] == "summary_response")
    assert response["usage"] == {"input_tokens": 20}
    assert response["finish_reason"] == "stop"
    assert response["text"] == "summary"


def _flight_cfg(tmp_path, session_file):
    from types import SimpleNamespace

    return SimpleNamespace(
        agent_id="voice", session_file=session_file, model="m", provider_id=None, provider_base_url=None,
        max_output_tokens=None,
        settings={"COMPACTION_LOG_DIR": str(tmp_path / "flights"),
                  "REQUEST_LOG_DIR": str(tmp_path / "logs")},
    )


def test_flight_snapshots_round_trip_saved_text_with_lone_surrogates(tmp_path):
    from js import compaction_flight, memory

    cfg = _flight_cfg(tmp_path, tmp_path / "s.jsonl")
    message = {"role": "user", "content": "filename: café-\udcff.txt"}
    memory.append_message(cfg.session_file, message)
    messages = memory.load_replay_messages(cfg.session_file)

    flight = compaction_flight.CompactionFlight(
        cfg, "system", messages, trigger={}, forced=True, focus=None,
        preserve_from=0, details={}, directory=compaction.flight_log_dir(cfg),
    )
    try:
        flight.finish("success", "system", messages)
    finally:
        flight.close()

    records = [json.loads(line) for line in flight.path.read_text(encoding="utf-8").splitlines()]
    assert [record["messages"] for record in records if record["event"] in {"before", "after"}] == [
        [message], [message],
    ]


def test_one_shot_flight_and_autolog_are_named_per_run_not_null(tmp_path):
    import os

    from js.compaction_flight import CompactionFlight

    cfg = _flight_cfg(tmp_path, Path(os.devnull))
    flight = CompactionFlight(cfg, "S", [], trigger={}, forced=True, focus=None, preserve_from=0, details={},
                              directory=compaction.flight_log_dir(cfg))
    flight.close()
    stem = f"voice-{os.getpid()}"
    assert flight.path.name.startswith(stem + "-")
    assert (tmp_path / "logs" / f"{stem}.log").exists()
    assert not (tmp_path / "logs" / "null.log").exists()
