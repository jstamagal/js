"""Regressions for the runtime cluster fixes (SWEEP rulings E/F, findings on
cancel turn_end, retry stream-close, and the byte-honest --debug-file trace)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import ai

from js import runtime
from js import colors as C
from js import model_client as MC
from js.config import Config
from js.stats import summarize_calls
from js.toolkit import ToolContext
from js.toolkit.core import Tool
from js.toolkit.registry import ToolRegistry, build_default_registry
from js.model_client import ModelStreamResult


def _cfg(tmp_path, agent="a"):
    base = tmp_path / "s" / agent
    return Config(
        agent_id=agent,
        agent_dir=base,
        model="offline-test-model",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=3,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=base / ".history",
        sessions_dir=base,
        session_file=base / "auto.jsonl",
        prompts_dir=tmp_path / "p" / agent,
    )


def _result(text="ok"):
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )


class _Recorder:
    """Minimal event sink: run_turn only reads emission.results / .hooks."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, **payload):
        self.events.append((event, payload))
        return SimpleNamespace(results=[], hooks=[])


# --------------------------------------------------------------------------
# RULING E / F: stats aggregation
# --------------------------------------------------------------------------

def test_summarize_calls_sums_token_denominators():
    # A tool-first turn: call 1 has no text token, call 2 does. prompt/cached/output
    # must share one cumulative denominator (ruling E) and ttft falls to the first
    # call that produced text (ruling F).
    calls = [
        {"prompt_tokens": 1000, "cached_tokens": 0, "output_tokens": 10, "stream_s": 1.0, "ttft_s": None, "finish_reason": "tool_calls"},
        {"prompt_tokens": 6000, "cached_tokens": 5500, "output_tokens": 20, "stream_s": 2.0, "ttft_s": 0.5, "finish_reason": "stop"},
    ]
    row = summarize_calls(calls)
    assert row["prompt_tokens"] == 7000
    assert row["cached_tokens"] == 5500
    assert row["output_tokens"] == 30
    assert row["cached_tokens"] <= row["prompt_tokens"]  # ratio is now meaningful
    assert row["ttft_s"] == 0.5


def test_summarize_calls_ttft_none_when_no_text_at_all():
    assert summarize_calls([{"ttft_s": None, "output_tokens": 0, "stream_s": 0.0}])["ttft_s"] is None


# --------------------------------------------------------------------------
# turn_end on cancel
# --------------------------------------------------------------------------

def test_turn_end_cancelled_emitted_on_cancel(tmp_path, monkeypatch):
    rec = _Recorder()

    async def slow(**kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(runtime.model_client, "stream_model_async", slow)
    cfg = _cfg(tmp_path)
    registry = build_default_registry(prompts_root=None)
    ctx = ToolContext(cwd=tmp_path)

    async def drive():
        task = asyncio.get_running_loop().create_task(
            runtime.run_turn_async(
                cfg, "SYS", [{"role": "user", "content": "hi"}],
                runtime.Telemetry(debug_log=None),
                tool_registry=registry, tool_context=ctx,
                suppress_output=True, event_hooks=rec,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())

    kinds = [e for e, _ in rec.events]
    assert "turn_start" in kinds
    ends = [p.get("reason") for e, p in rec.events if e == "turn_end"]
    assert ends == ["cancelled"]  # exactly one, balancing turn_start


# --------------------------------------------------------------------------
# retry closes the partial text stream
# --------------------------------------------------------------------------

def test_partial_text_closed_before_retry(tmp_path, monkeypatch, capsys):
    n = {"i": 0}

    def stub(**kwargs):
        n["i"] += 1
        on_text = kwargs["on_text"]
        if n["i"] == 1:
            on_text("PARTIAL")  # tokens already reached stdout, color left open
            raise ai.ProviderAPIError("boom", provider="test", is_retryable=True)
        on_text("FINAL")
        return _result("FINAL")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stub)
    monkeypatch.setattr(runtime, "_backoff", lambda a: 0.0)
    cfg = _cfg(tmp_path)
    registry = build_default_registry(prompts_root=None)
    ctx = ToolContext(cwd=tmp_path)

    runtime.run_turn(
        cfg, "SYS", [{"role": "user", "content": "hi"}],
        runtime.Telemetry(debug_log=None),
        tool_registry=registry, tool_context=ctx, suppress_output=False,
    )

    out = capsys.readouterr().out
    assert "PARTIAL" in out and "FINAL" in out
    # A RESET terminates the partial text before the retried text is streamed.
    assert C.RESET in out[out.index("PARTIAL"):out.index("FINAL")]


# --------------------------------------------------------------------------
# --debug-file request trace is byte-honest (finding 62)
# --------------------------------------------------------------------------

def test_request_trace_is_byte_honest(capsys):
    system = "S" * 9000  # longer than the old 8000-char clip
    messages = [ai.system_message(system), ai.user_message("hello-user")]
    tools = MC.tool_specs_to_ai_tools([
        {
            "type": "function",
            "function": {
                "name": "shell",
                "description": "RUN A COMMAND IN BASH AND RETURN ITS OUTPUT",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
            },
        }
    ])

    MC._emit_request_trace(
        model_id="m", provider_id="deepseek", provider_base_url="https://x",
        params=None, messages=messages, tools=tools, dump_schemas=True, dump_from=0,
    )
    out = capsys.readouterr().out
    assert system in out                                    # system prompt unclipped
    assert "RUN A COMMAND IN BASH AND RETURN ITS OUTPUT" in out  # full description, not just name
    assert '"command"' in out                               # parameter schema present
    assert "hello-user" in out                              # messages dumped


def test_request_trace_skips_schemas_on_followup(capsys):
    messages = [ai.system_message("SYS"), ai.user_message("u"), ai.assistant_message("a")]
    MC._emit_request_trace(
        model_id="m", provider_id=None, provider_base_url=None,
        params=None, messages=messages, tools=[], dump_schemas=False, dump_from=2,
    )
    out = capsys.readouterr().out
    assert "TOOL SCHEMAS" not in out
    assert "SYSTEM PROMPT" not in out
    assert "MESSAGES (+1)" in out  # only the one new message beyond dump_from


# --------------------------------------------------------------------------
# dispatch-layer clip carries the same visible marker as the subagent layer
# --------------------------------------------------------------------------

def test_cap_result_marks_only_when_it_shortens():
    assert runtime._cap_result("short", 100) == "short"           # untouched
    assert runtime._cap_result("x" * 100, 0) == "x" * 100          # 0 == unlimited
    clipped = runtime._cap_result("x" * 100, 10)
    assert clipped.startswith("x" * 10)
    assert "[truncated: limits.max_tool_result_bytes (10) reached]" in clipped


def test_dispatch_marks_truncated_leaf_result():
    big = "Z" * 500

    def _handler(context=None, **kwargs):
        return big

    tool = Tool(name="bigtool", description="", handler=_handler, params={})
    registry = ToolRegistry(tools=(tool,), aliases={})
    _args, result = runtime._dispatch(
        "bigtool", "{}", runtime.Telemetry(debug_log=None),
        cap_bytes=50, registry=registry, tool_context=ToolContext(),
    )
    assert result.startswith("Z" * 50)
    assert "[truncated: limits.max_tool_result_bytes (50) reached]" in result
