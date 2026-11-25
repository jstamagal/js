"""An unexpected exception inside a turn ends that turn.

In the REPL the operator sees the error, no turn is left active, the session
file holds what the REPL holds, and the next typed line runs a normal turn.
A `-p` run exits non-zero with the error, and under --json ends with its
error record."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import ai
import ai.types.usage
import pytest
from repl_driver import run_async

from js import attach, cli, memory, runtime, supervisor, turn_budget, turn_dispatch
from js.config import from_env
from js.memory import load_replay_messages
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit.core import Tool, ToolContext
from js.toolkit.registry import ToolRegistry

INJECTED = "injected-turn-failure"


def _text(text: str) -> ModelStreamResult:
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )


def _call(name: str, call_id: str) -> ModelStreamResult:
    message = ai.types.messages.Message(
        role="assistant",
        parts=[ai.types.messages.ToolCallPart(tool_call_id=call_id, tool_name=name, tool_args="{}")],
    )
    return ModelStreamResult(
        text="",
        tool_calls=[ModelToolCall(id=call_id, name=name, arguments="{}")],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1),
        finish_reason="tool_calls",
        assistant_message=message,
    )


def _last_user_text(convo) -> str:
    users = [m for m in convo if m.role == "user"]
    return "".join(part.text for part in users[-1].parts if part.kind == "text") if users else ""


async def _until(what: str, predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


class _Failing:
    """While `broken` is set, the patched step raises AttributeError."""

    def __init__(self) -> None:
        self.broken = threading.Event()
        self.broken.set()
        self.raised = threading.Event()

    def fail(self) -> None:
        if self.broken.is_set():
            self.raised.set()
            raise AttributeError(INJECTED)


def _break_request(monkeypatch, failing: _Failing) -> None:
    real_fit = turn_budget.TurnBudget.fit

    async def fit(self, *args, **kwargs):
        failing.fail()
        return await real_fit(self, *args, **kwargs)

    monkeypatch.setattr(turn_budget.TurnBudget, "fit", fit)


def _break_dispatch(monkeypatch, failing: _Failing) -> None:
    real_dispatch = turn_dispatch.dispatch_batch

    async def dispatch_batch(*args, **kwargs):
        failing.fail()
        return await real_dispatch(*args, **kwargs)

    monkeypatch.setattr(turn_dispatch, "dispatch_batch", dispatch_batch)


def _break_persist(monkeypatch, failing: _Failing) -> None:
    real_persist = memory.persist_messages

    def persist_messages(*args, **kwargs):
        failing.fail()
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(memory, "persist_messages", persist_messages)


def _break_user_message(monkeypatch, failing: _Failing) -> None:
    real_build = attach.build_user_message

    def build_user_message(*args, **kwargs):
        failing.fail()
        return real_build(*args, **kwargs)

    monkeypatch.setattr(attach, "build_user_message", build_user_message)


def _answer_with(monkeypatch, *, point: str, failing: _Failing) -> list[str]:
    """The model: it answers each user line, except that the turn on "first"
    fails mid-stream at the "stream" point and calls the `nap` tool at the
    "dispatch" point. Returns the user lines it answered, in order."""
    registry = ToolRegistry(tools=(Tool("nap", "Nap.", lambda: "rested", {}),), aliases={})
    real_run_turn_async = runtime.run_turn_async
    answered: list[str] = []

    async def run_turn_async(cfg, system, messages, telemetry, **kwargs):
        kwargs["tool_registry"] = registry
        kwargs["event_hooks"] = None
        kwargs["mcp_host"] = None
        await real_run_turn_async(cfg, system, messages, telemetry, **kwargs)

    async def model(**kwargs):
        convo = kwargs["messages"]
        last = _last_user_text(convo)
        if "first" in last and convo[-1].role == "user":
            if point == "stream":
                kwargs["on_text"]("partial answer ")
                failing.fail()
            if point == "dispatch":
                return _call("nap", "call_nap")
        answered.append(last)
        return _text(f"reply to {last}")

    monkeypatch.setattr(runtime, "run_turn_async", run_turn_async)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", model)
    return answered


def _run(monkeypatch, tmp_path, *, point: str, failing: _Failing) -> dict:
    """Type "first" (its turn fails at `point`), wait for that turn to be
    over, then type "second" with the failure gone and wait for its answer.
    Returns what was observed along the way and the REPL's final state."""
    seen: dict = {}

    async def script(enter):
        sup = supervisor.get_current()
        await enter("first")
        await _until("the injected failure", failing.raised.is_set)
        await _until("the failed turn to end", lambda: not sup.turn_active())
        seen["jobs_after_failure"] = [job.label for job in sup.jobs("turn")]
        failing.broken.clear()
        await enter("second")
        await _until("the second line's answer", lambda: any("second" in t for t in answered))
        await _until("the second turn to end", lambda: not sup.turn_active())

    monkeypatch.delenv("JS_DEFAULT_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    answered = _answer_with(monkeypatch, point=point, failing=failing)

    seen["state"] = run_async(monkeypatch, from_env(), script)
    [session] = list((tmp_path / ".js" / "sessions").rglob("*.jsonl"))
    seen["disk"] = load_replay_messages(session)
    return seen


def _shape(messages: list[dict]) -> list[tuple[str, str]]:
    """Each message's role, with its text for a user or assistant message."""
    return [(m["role"], m["content"] if m["role"] in ("user", "assistant") and isinstance(m.get("content"), str)
             else "") for m in messages]


SECOND_ONLY = [("user", "second"), ("assistant", "reply to second")]

RAISE_POINTS = [
    ("user_message", _break_user_message),
    ("request", _break_request),
    ("stream", None),
    ("dispatch", _break_dispatch),
    ("persist", _break_persist),
]


# What the session keeps of the turn that failed at each point.
KEPT = {
    # The failed turn produced nothing the history keeps: it is dropped.
    "user_message": SECOND_ONLY,
    "request": SECOND_ONLY,
    "stream": SECOND_ONLY,
    # Its tool call was recorded: the turn is kept, the call answered as cut off.
    "dispatch": [("user", "first"), ("assistant", ""), ("tool", ""), *SECOND_ONLY],
    # Its answer was in memory when saving failed; the next save writes it.
    "persist": [("user", "first"), ("assistant", "reply to first"), *SECOND_ONLY],
}


@pytest.mark.parametrize("point, breaks", RAISE_POINTS)
def test_unexpected_exception_ends_the_turn_and_the_next_line_runs(monkeypatch, tmp_path, capsys,
                                                                   point, breaks):
    failing = _Failing()
    if breaks is not None:
        breaks(monkeypatch, failing)

    seen = _run(monkeypatch, tmp_path, point=point, failing=failing)

    assert INJECTED in capsys.readouterr().out
    assert seen["jobs_after_failure"] == []
    assert _shape(seen["disk"]) == _shape(seen["state"].messages)
    assert _shape(seen["disk"]) == KEPT[point]


@pytest.mark.parametrize("point, breaks", RAISE_POINTS)
@pytest.mark.parametrize("flags", [[], ["--json-stream"]], ids=["plain", "json"])
def test_unexpected_exception_in_a_prompt_run_exits_non_zero_with_the_error(monkeypatch, tmp_path, capsys,
                                                                            point, breaks, flags):
    class Terminal:
        def isatty(self):
            return True

    failing = _Failing()
    if breaks is not None:
        breaks(monkeypatch, failing)
    monkeypatch.setattr(cli.sys, "stdin", Terminal())
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))
    monkeypatch.chdir(tmp_path)
    _answer_with(monkeypatch, point=point, failing=failing)

    try:
        code = cli.main(["-p", "first", *flags])
        escaped = ""
    except AttributeError as error:
        # Out of main, the interpreter prints its traceback and exits 1.
        code, escaped = 1, str(error)

    captured = capsys.readouterr()
    assert failing.raised.is_set()
    assert code != 0
    assert INJECTED in captured.err + escaped
    if flags:
        events = [json.loads(line) for line in captured.out.splitlines()]
        assert [e["type"] for e in events if e["type"] in ("start", "error", "end")] == ["start", "error", "end"]
        assert INJECTED in next(e for e in events if e["type"] == "error")["message"]
        assert events[-1]["ok"] is False and events[-1]["exit_code"] == code
