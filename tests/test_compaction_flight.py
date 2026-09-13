from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from js import cli, compaction, runtime
from test_config_compaction_layers import _compact_test_cfg
from test_cli_prompt_mode import _auto_state


def read_flight(cfg):
    files = list(Path(cfg.settings["compact"]["flight_log_dir"]).glob("*.jsonl"))
    assert len(files) == 1
    return files[0], [json.loads(line) for line in files[0].read_text().splitlines()]


@pytest.mark.parametrize("failure", [None, RuntimeError("summary failed"), asyncio.CancelledError()])
def test_flight_persists_before_after_and_terminal_outcome(monkeypatch, tmp_path, capsys, failure):
    cfg = _compact_test_cfg(tmp_path, {"flight_log_dir": str(tmp_path / "flights"), "tail_tokens": 1})
    cfg.settings["provider"] = {"api_key": "private-key"}
    original = [{"role": "user", "content": "old content"}, {"role": "user", "content": "new content"}]
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
        asyncio.run(compaction.compact_now(cfg, "SYSTEM", messages, forced=True))
    else:
        with pytest.raises(type(failure)):
            asyncio.run(compaction.compact_now(cfg, "SYSTEM", messages, forced=True))
        assert messages == original
    path, records = read_flight(cfg)
    assert records[0]["settings"]["provider"]["api_key"] == "[redacted]"
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
    cfg = _compact_test_cfg(tmp_path, {"context_window": 1000000})
    state = _auto_state()
    state["settings"] = {"compact": {"context_window": 128000}}
    captured = []
    monkeypatch.setattr(compaction, "maybe_auto_compact", lambda active, *args: captured.append(active) or compaction.AutoCompactOutcome())
    cli._maybe_auto_compact(cfg, state)
    assert captured[0].settings["compact"]["context_window"] == 128000
    state["settings"]["compact"]["context_window"] = 1000000
    cli._maybe_auto_compact(cfg, state)
    assert captured[-1].settings["compact"]["context_window"] == 1000000


def test_same_repl_set_override_updates_anchor_then_request_budget(monkeypatch, tmp_path, capsys):
    from dataclasses import replace
    from js import settings
    from js.toolkit import ToolContext
    from js.toolkit.registry import build_default_registry
    from test_lazy_tool_discovery import _result

    cfg = _compact_test_cfg(tmp_path, {})
    cfg = replace(cfg, trace=True, max_output_tokens=128, provider_id="openai",
                  provider_api_key="offline", provider_base_url="http://127.0.0.1:1/v1")
    live = settings.seed_defaults()
    live["compact"]["context_window"] = 128000
    state = {"settings": live, "model": cfg.model, "messages": [], "system": "SYSTEM"}
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *args: None)
    captured = []

    class Events:
        trace_sink = None
        transcript_log = None
        def event(self, kind, **fields):
            if kind == "context_budget":
                captured.append(fields["context_window"])

    async def sdk(**kwargs):
        return _result(text="ok")

    monkeypatch.setattr(runtime.model_client, "_stream_async", sdk)
    context = ToolContext(cwd=tmp_path)
    registry = build_default_registry().select([])

    async def conversation():
        state["messages"].append({"role": "user", "content": "first"})
        await runtime.run_turn_async(cli._cfg_for_live_state(cfg, state), "SYSTEM", state["messages"],
                                     Events(), tool_context=context, tool_registry=registry)
        assert captured[-1] == 128000
        capsys.readouterr()
        assert cli._handle_command("/set compact.context_window 1000000", state, cfg)
        immediate = capsys.readouterr().out
        assert "ctx=1000000" in immediate
        assert state["settings"]["compact"]["context_window"] == 1000000
        state["messages"].append({"role": "user", "content": "second"})
        await runtime.run_turn_async(cli._cfg_for_live_state(cfg, state), "SYSTEM", state["messages"],
                                     Events(), tool_context=context, tool_registry=registry)
        assert captured[-1] == 1000000
        assert "ctx=1000000" in capsys.readouterr().out

    asyncio.run(conversation())


def test_summary_request_and_response_are_recorded(monkeypatch, tmp_path):
    from types import SimpleNamespace
    cfg = _compact_test_cfg(tmp_path, {"flight_log_dir": str(tmp_path / "flights"), "tail_tokens": 1})

    async def stream(**kwargs):
        assert kwargs["trace_request"] is True
        kwargs["trace_sink"].write("provider request trace")
        kwargs["on_text"]("summary chunk")
        return SimpleNamespace(text="summary", usage={"input_tokens": 20}, finish_reason="stop")

    monkeypatch.setattr(compaction.model_client, "stream_model_async", stream)
    asyncio.run(compaction.compact_now(cfg, "system", [{"role": "user", "content": "old"},
                                                       {"role": "user", "content": "new"}], forced=True))
    path, records = read_flight(cfg)
    assert any(r["event"] == "summary_trace" and r["text"] == "provider request trace" for r in records)
    assert any(r["event"] == "summary_chunk" and r["text"] == "summary chunk" for r in records)
    response = next(r for r in records if r["event"] == "summary_response")
    assert response["usage"] == {"input_tokens": 20}
    assert response["finish_reason"] == "stop"
    assert response["text"] == "summary"
