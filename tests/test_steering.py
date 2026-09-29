"""Input typed while a turn runs (runtime.steer).

now: the line reaches the model at the running turn's next tool boundary, as a
user message after the tool results; a turn with no boundary left gets it as
one message after it ends. batch: lines typed during a turn go in as ONE
message after it. /flush drops pending lines without touching the turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

import ai
import ai.types.usage
from ai.providers import history_utils

from js import cli, runtime, settings
from js.config import Config
from js.memory import load_messages
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import ToolContext
from js.toolkit.core import Tool
from js.toolkit.registry import ToolRegistry


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


def _user_texts(convo) -> list[str]:
    return [
        "".join(part.text for part in message.parts if part.kind == "text")
        for message in convo
        if message.role == "user"
    ]


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


# --------------------------------------------------------------------------
# runtime: the steer hook at the tool boundary
# --------------------------------------------------------------------------

def test_steered_message_follows_the_tool_results_and_precedes_the_next_call(monkeypatch, tmp_path):
    seen: list[list] = []

    async def model(**kwargs):
        convo = kwargs["messages"]
        history_utils.validate(convo)
        seen.append(list(convo))
        return _call("probe", "call_1") if len(seen) == 1 else _text("done")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", model)
    registry = ToolRegistry(tools=(Tool("probe", "Probe.", lambda: "probed", {}),), aliases={})
    offered = [{"role": "user", "content": "turn left instead", "steered": True}]
    messages = [{"role": "user", "content": "go"}]

    runtime.run_turn(
        _cfg(tmp_path), "SYS", messages, runtime.Telemetry(debug_log=None),
        tool_registry=registry, tool_context=ToolContext(cwd=tmp_path), suppress_output=True,
        steer=lambda: offered.pop() if offered else None,
    )

    assert len(seen) == 2
    assert [m.role for m in seen[1][-3:]] == ["assistant", "tool", "user"]
    assert _user_texts(seen[1])[-1] == "turn left instead"
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user", "assistant"]
    assert messages[3]["content"] == "turn left instead"


def test_a_steered_message_does_not_start_a_new_turn_for_compaction():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "user", "content": "also this", "steered": True},
    ]
    assert runtime._last_user_message_index(messages) == 0


def test_steer_knob_accepts_the_three_modes_only():
    spec = next(s for s in settings.REGISTRY if s.key == "runtime.steer")
    assert spec.default == "now"
    for mode in ("now", "batch", "one", "BATCH"):
        value, error = settings.coerce_value(spec, mode)
        assert error is None and value == mode.lower()
    assert settings.coerce_value(spec, "sideways")[1] is not None


# --------------------------------------------------------------------------
# REPL: lines typed during a turn
# --------------------------------------------------------------------------

class _Harness:
    """Drives the async REPL headless: a script of on_line calls, a model stub,
    and a `hold` tool that blocks until the script releases it."""

    def __init__(self, monkeypatch, tmp_path, *, first_call_uses_tool: bool = True):
        self.tmp_path = tmp_path
        self.calls: list[list] = []
        self.hold_started = threading.Event()
        self.hold_release = threading.Event()
        self._first_call_uses_tool = first_call_uses_tool
        self.text_gate: asyncio.Event | None = None
        self.text_gate_entered: asyncio.Event | None = None

        def hold() -> str:
            self.hold_started.set()
            assert self.hold_release.wait(10)
            return "held"

        registry = ToolRegistry(tools=(Tool("hold", "Hold.", hold, {}),), aliases={})
        real_run_turn_async = runtime.run_turn_async

        async def run_turn_async_with_hold(cfg, system, messages, telemetry, **kwargs):
            kwargs["tool_registry"] = registry
            kwargs["event_hooks"] = None
            kwargs["mcp_host"] = None
            await real_run_turn_async(cfg, system, messages, telemetry, **kwargs)

        async def model(**kwargs):
            convo = kwargs["messages"]
            history_utils.validate(convo)
            self.calls.append(list(convo))
            if len(self.calls) == 1:
                if self._first_call_uses_tool:
                    return _call("hold", "call_hold")
                self.text_gate_entered.set()
                await self.text_gate.wait()
            return _text(f"reply {len(self.calls)}")

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("JS_AGENT", raising=False)
        monkeypatch.delenv("JS_SESSION", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(cli.screen, "capture_stdio", lambda *a, **k: contextlib.nullcontext())
        monkeypatch.setattr(cli.runtime, "run_turn_async", run_turn_async_with_hold)
        monkeypatch.setattr(runtime.model_client, "stream_model_async", model)
        self.monkeypatch = monkeypatch

    def run(self, script) -> None:
        harness = self

        class AppStub:
            def __init__(self, on_line, on_eof):
                self.on_line, self.on_eof = on_line, on_eof

            async def run_async(self):
                harness.text_gate = asyncio.Event()
                harness.text_gate_entered = asyncio.Event()
                await script(self.on_line, harness)
                self.on_eof()

            def exit(self):
                pass

            def invalidate(self):
                pass

        self.monkeypatch.setattr(
            cli.screen, "build_app",
            lambda *, on_line, on_eof, **_: (AppStub(on_line, on_eof), cli.screen.Scrollback()),
        )
        assert cli.main([]) == 0

    async def wait_hold(self) -> None:
        while not self.hold_started.is_set():
            await asyncio.sleep(0.01)

    def transcript(self) -> str:
        found = list((self.tmp_path / ".local" / "share" / "js" / "transcript").rglob("*.log"))
        assert len(found) == 1, found
        return found[0].read_text()

    def session(self) -> list[dict]:
        found = list((self.tmp_path / ".local" / "share" / "js" / "sessions").rglob("*.jsonl"))
        assert len(found) == 1, found
        return load_messages(found[0])


def test_steer_now_reaches_the_model_before_its_next_call(monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path)

    async def script(on_line, h):
        await on_line("first")
        await h.wait_hold()
        await on_line("actually check the logs")
        await on_line("and the config")
        h.hold_release.set()

    h.run(script)

    # One turn: the tool call, then one model call that already has the lines.
    assert len(h.calls) == 2
    assert [m.role for m in h.calls[1][-3:]] == ["assistant", "tool", "user"]
    assert _user_texts(h.calls[1])[-1] == "actually check the logs\nand the config"
    session = h.session()
    assert [m["role"] for m in session] == ["user", "assistant", "tool", "user", "assistant"]
    assert session[3]["content"] == "actually check the logs\nand the config"
    assert session[3]["steered"] is True


def test_steer_now_without_a_boundary_left_arrives_after_the_turn(monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path, first_call_uses_tool=False)

    async def script(on_line, h):
        await on_line("first")
        await h.text_gate_entered.wait()
        await on_line("one more thing")
        await on_line("and another")
        h.text_gate.set()

    h.run(script)

    assert len(h.calls) == 2
    assert _user_texts(h.calls[1])[-1] == "one more thing\nand another"
    assert [m["role"] for m in h.session()] == ["user", "assistant", "user", "assistant"]


def test_steer_batch_sends_lines_typed_during_a_turn_as_one_message(monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path)

    async def script(on_line, h):
        await on_line("/set runtime.steer batch")
        await on_line("first")
        await h.wait_hold()
        await on_line("second")
        await on_line("third")
        h.hold_release.set()

    h.run(script)

    # Turn one: tool call + answer. Turn two: both queued lines, one message.
    assert len(h.calls) == 3
    assert "second" not in "".join(_user_texts(h.calls[1]))
    assert _user_texts(h.calls[2])[-1] == "second\nthird"
    session = h.session()
    assert [m["role"] for m in session] == ["user", "assistant", "tool", "assistant", "user", "assistant"]


def test_steer_one_runs_a_turn_per_line(monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path)

    async def script(on_line, h):
        await on_line("/set runtime.steer one")
        await on_line("first")
        await h.wait_hold()
        await on_line("second")
        await on_line("third")
        h.hold_release.set()

    h.run(script)

    assert len(h.calls) == 4
    assert _user_texts(h.calls[2])[-1] == "second"
    assert _user_texts(h.calls[3])[-1] == "third"


def test_flush_drops_lines_waiting_to_steer(monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path)

    async def script(on_line, h):
        await on_line("first")
        await h.wait_hold()
        await on_line("never mind this")
        await on_line("/flush")
        h.hold_release.set()

    h.run(script)

    assert len(h.calls) == 2
    assert all("never mind" not in text for call in h.calls for text in _user_texts(call))
    assert [m["role"] for m in h.session()] == ["user", "assistant", "tool", "assistant"]


def test_steered_line_is_in_the_transcript_where_the_model_receives_it(monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path)

    async def script(on_line, h):
        await on_line("first")
        await h.wait_hold()
        await on_line("actually check the logs")
        h.hold_release.set()

    h.run(script)

    # The line was typed before the tool returned; the transcript has it after
    # the tool result, where it entered the conversation.
    log = h.transcript()
    assert log.count("actually check the logs") == 1
    assert log.index("first") < log.index("held") < log.index("actually check the logs")


def test_steered_line_goes_through_the_input_event(monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path)
    inputs: list[str] = []
    real_emit = cli._emit_repl_event

    def recording_emit(state, telemetry, event, **payload):
        if event == "input":
            inputs.append(payload["text"])
        return real_emit(state, telemetry, event, **payload)

    monkeypatch.setattr(cli, "_emit_repl_event", recording_emit)

    async def script(on_line, h):
        await on_line("first")
        await h.wait_hold()
        await on_line("actually check the logs")
        h.hold_release.set()

    h.run(script)

    assert inputs == ["first", "actually check the logs"]


def test_steered_skill_line_carries_the_skill(monkeypatch, tmp_path):
    skill = tmp_path / ".js" / "skills" / "secret" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\ndisable-model-invocation: true\n---\nuser-only body\n", encoding="utf-8")
    h = _Harness(monkeypatch, tmp_path)

    async def script(on_line, h):
        await on_line("first")
        await h.wait_hold()
        await on_line("/skill secret check the plan")
        h.hold_release.set()

    h.run(script)

    assert len(h.calls) == 2
    steered = _user_texts(h.calls[1])[-1]
    assert "user-only body" in steered
    assert "check the plan" in steered
