"""A running tool never takes the input line away from the operator.

The async REPL's input line and the active turn share one event loop. A tool
call runs off that loop, cannot read the terminal the input line reads, and
dies with the turn when ^C cancels it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from dataclasses import replace

import ai
import ai.types.usage
import pytest

from js import cli, runtime
from js.config import Config, from_env
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import ToolContext
from js.toolkit import process_net
from js.toolkit.core import Tool
from js.toolkit.registry import ToolRegistry
from repl_driver import run_async


def _cfg(tmp_path):
    base = tmp_path / "s" / "a"
    return Config(
        agent_id="a",
        agent_dir=base,
        model="offline-test-model",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=4,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=base / ".history",
        sessions_dir=base,
        session_file=base / "auto.jsonl",
        prompts_dir=tmp_path / "p" / "a",
    )


def _text(text: str) -> ModelStreamResult:
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )


def _call(name: str, args: str, call_id: str) -> ModelStreamResult:
    call = ModelToolCall(id=call_id, name=name, arguments=args)
    message = ai.types.messages.Message(
        role="assistant",
        parts=[ai.types.messages.ToolCallPart(tool_call_id=call_id, tool_name=name, tool_args=args)],
    )
    return ModelStreamResult(
        text="",
        tool_calls=[call],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1),
        finish_reason="tool_calls",
        assistant_message=message,
    )


def test_second_line_is_processed_while_a_tool_runs(monkeypatch, tmp_path):
    """Drive one line through the Enter handler into a turn whose tool blocks
    until the test releases it; a second line entered meanwhile is handled
    before that turn finishes."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def nap() -> str:
        started.set()
        release.wait(30)
        finished.set()
        return "rested"

    nap_registry = ToolRegistry(tools=(Tool("nap", "Sleep.", nap, {}),), aliases={})
    real_run_turn_async = runtime.run_turn_async

    async def model(**kwargs):
        convo = kwargs["messages"]
        if convo[-1].role == "user" and "first" in str(convo[-1].parts):
            return _call("nap", "{}", "call_nap")
        return _text("ok")

    async def run_turn_async_with_nap(cfg, system, messages, telemetry, **kwargs):
        kwargs["tool_registry"] = nap_registry
        kwargs["event_hooks"] = None
        kwargs["mcp_host"] = None
        await real_run_turn_async(cfg, system, messages, telemetry, **kwargs)

    seen: dict[str, bool] = {}

    async def script(on_line):
        await on_line("first")
        while not started.is_set():
            await asyncio.sleep(0.01)
        await asyncio.wait_for(on_line("second"), timeout=10)
        seen["tool_finished_before_second_line"] = finished.is_set()
        release.set()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.runtime, "run_turn_async", run_turn_async_with_nap)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", model)

    run_async(monkeypatch, from_env(), script)

    assert seen == {"tool_finished_before_second_line": False}
    assert finished.is_set()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fd semantics")
def test_shell_command_does_not_read_the_operators_terminal(tmp_path):
    """Whatever js's stdin is (the terminal the input line reads), a shell
    command started by the tool gets none of it and does not wait on it."""
    read_end, write_end = os.pipe()
    os.write(write_end, b"keystrokes-for-the-input-line\n")
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        started = time.monotonic()
        out = process_net.shell("cat", timeout=5, context=ToolContext(cwd=tmp_path))
        elapsed = time.monotonic() - started
    finally:
        os.dup2(saved, 0)
        os.close(saved)
    try:
        assert "exit=0" in out
        assert "keystrokes" not in out
        assert elapsed < 4
        os.set_blocking(read_end, False)
        assert os.read(read_end, 1024) == b"keystrokes-for-the-input-line\n"
    finally:
        os.close(read_end)
        os.close(write_end)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_cancelling_a_turn_kills_the_running_shell_command(monkeypatch, tmp_path):
    marker = tmp_path / "pid"
    command = f"echo $$ > {marker}; exec sleep 60"

    async def model(**kwargs):
        return _call("run", f'{{"command": "{command}", "timeout": 60}}', "call_sleep")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", model)
    shell_tool = next(tool for tool in process_net.tools() if tool.name == "shell")
    # Published under its own name so the turn surface does not defer it.
    shell_only = ToolRegistry(tools=(replace(shell_tool, name="run"),), aliases={})
    ctx = ToolContext(cwd=tmp_path)
    messages = [{"role": "user", "content": "sleep"}]

    async def drive() -> float:
        task = asyncio.get_running_loop().create_task(
            runtime.run_turn_async(
                _cfg(tmp_path), "SYS", messages, runtime.Telemetry(debug_log=None),
                tool_registry=shell_only, tool_context=ctx, suppress_output=True,
            )
        )
        deadline = time.monotonic() + 10
        while not (marker.exists() and marker.read_text().strip()):
            assert not task.done() and time.monotonic() < deadline
            await asyncio.sleep(0.02)
        cancelled_at = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - cancelled_at

    took = asyncio.run(drive())
    pid = int(marker.read_text())
    assert took < 5
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    results = [m for m in messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in results] == ["call_sleep"]
