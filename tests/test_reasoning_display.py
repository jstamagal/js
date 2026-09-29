"""Reasoning has its own live display channel and remains in session history."""

import asyncio
import io
import json
from types import SimpleNamespace

import ai
import ai.types.events as events
import pytest
import httpx2

from js import cli, config, memory, model_client, runtime, screen, settings, transcript
from js.toolkit import ToolContext


THOUGHT = " first thought\n  next thought: λ\n"


def scripted(monkeypatch, *, cancel=False, before_answer=None):
    def stream(**kwargs):
        async def generate():
            yield events.StreamStart()
            yield events.ReasoningStart(block_id="reason")
            yield events.ReasoningDelta(chunk=THOUGHT, block_id="reason")
            if before_answer:
                before_answer()
            if cancel:
                raise asyncio.CancelledError()
            yield events.ReasoningEnd(block_id="reason")
            yield events.TextStart(block_id="text")
            yield events.TextDelta(chunk="answer", block_id="text")
            yield events.TextEnd(block_id="text")
            yield events.StreamEnd()
        return ai.models.Stream(generate())
    monkeypatch.setattr(ai, "stream", stream)


def test_provider_stream_delivers_reasoning_before_answer(monkeypatch):
    seen = []
    scripted(monkeypatch)
    result = model_client.stream_model(
        model_id="qwen-test", provider_id="openai", provider_base_url="http://local.test/v1",
        provider_api_key="fixture", messages=[ai.user_message("first")], tools=None,
        max_output_tokens=None, reasoning_effort=None,
        on_reasoning=lambda chunk: seen.append(("reasoning", chunk)),
        on_text=lambda chunk: seen.append(("text", chunk)),
    )
    assert seen == [("reasoning", THOUGHT), ("text", "answer")]
    assert result.reasoning == THOUGHT
    assert result.text == "answer"


@pytest.mark.parametrize("level", [0, 1, 2, 3])
@pytest.mark.parametrize("suppressed", [False, True])
def test_display_is_separate_from_answer_transcript_and_session(monkeypatch, tmp_path, level, suppressed):
    cfg = config.from_env(extras=[
        "model.id=qwen-test", "provider.id=openai", "provider.base_url=http://local.test/v1",
        "provider.api_key=fixture", "model.max_output_tokens=64", "model.context_window=262144",
        "model.vision=off", f"ui.reasoning={level}",
    ])
    out, err, log = io.StringIO(), io.StringIO(), io.StringIO()
    sink = transcript.TranscriptLogSink([log], [])
    monkeypatch.setattr(runtime.sys, "stdout", transcript.TranscriptTee(out, lambda: sink))
    monkeypatch.setattr(runtime.sys, "stderr", transcript.TranscriptTee(err, lambda: sink))

    def before_answer():
        assert (THOUGHT in transcript.strip_ansi(err.getvalue())) is (level > 0 and not suppressed)
        assert THOUGHT not in out.getvalue()

    scripted(monkeypatch, before_answer=before_answer)
    messages = [{"role": "user", "content": "first"}]
    runtime.run_turn(
        cfg, "system", messages, runtime.Telemetry(None, transcript_log=sink),
        trace_override=False, suppress_output=suppressed, tool_context=ToolContext(cwd=tmp_path),
    )
    assert THOUGHT not in out.getvalue()
    assert THOUGHT not in log.getvalue()
    assert messages[-1]["reasoning_content"] == THOUGHT
    memory.persist_messages(cfg.session_file, messages)
    saved = cfg.session_file.read_bytes()
    reloaded = memory.load_messages(cfg.session_file, preserve_reasoning=True)
    assert reloaded[-1]["reasoning_content"] == THOUGHT
    assert model_client.history_to_ai_messages("system", reloaded, provider_id="openai")[-1].reasoning == THOUGHT
    memory.persist_messages(cfg.session_file, reloaded)
    assert cfg.session_file.read_bytes() == saved


def test_cancel_during_reasoning_retains_partial_history(monkeypatch, tmp_path):
    cfg = config.from_env(extras=[
        "model.id=qwen-test", "provider.id=openai", "provider.base_url=http://local.test/v1",
        "provider.api_key=fixture", "model.max_output_tokens=64", "model.context_window=262144",
        "model.vision=off", "ui.reasoning=0",
    ])
    scripted(monkeypatch, cancel=True)
    messages = [{"role": "user", "content": "first"}]
    with pytest.raises(asyncio.CancelledError):
        runtime.run_turn(
            cfg, "system", messages, runtime.Telemetry(None), trace_override=False,
            suppress_output=True, tool_context=ToolContext(cwd=tmp_path),
        )
    assert len(messages) == 2
    assert messages[-1]["reasoning_content"] == THOUGHT
    assert messages[-1]["incomplete_reason"] == "cancelled"
    memory.persist_messages(cfg.session_file, messages)
    assert memory.load_messages(cfg.session_file, preserve_reasoning=True)[-1] == messages[-1]


def test_reasoning_level_is_registered_validated_and_saved(tmp_path):
    spec = settings.SPEC_BY_KEY["ui.reasoning"]
    assert spec.default == 2
    for level in range(4):
        assert settings.coerce_value(spec, str(level)) == (level, None)
    assert settings.coerce_value(spec, "-1")[1]
    assert settings.coerce_value(spec, "4")[1]
    path = tmp_path / "jsrc"
    settings.save_settings_to_jsrc(path, {"ui": {"reasoning": 1}})
    loaded = settings.collect_settings(config_paths=[path], env={})
    assert settings.get_dotted(loaded, ("ui", "reasoning")) == 1


@pytest.mark.parametrize("level", [1, 2, 3])
def test_scrollback_collapse_restores_reasoning_without_losing_other_output(level):
    scroll = screen.Scrollback()
    scroll.append("before\n")
    block = scroll.reasoning(level)
    block.append(THOUGHT)
    assert THOUGHT in transcript.strip_ansi(scroll.buffer.text)
    scroll.append("queued input\n")
    block.answer_started()
    scroll.append("answer\n")
    block.finish(17)
    visible = transcript.strip_ansi(scroll.buffer.text)
    assert (THOUGHT in visible) is (level != 1)
    assert "before\n" in visible and "queued input\nanswer\n" in visible
    scroll.toggle_reasoning()
    assert (THOUGHT in transcript.strip_ansi(scroll.buffer.text)) is (level == 1)
    scroll.toggle_reasoning()
    assert transcript.strip_ansi(scroll.buffer.text) == visible
    assert block.text == THOUGHT


def test_ctrl_r_keeps_input_intact_and_restores_reasoning():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    async def drive():
        async def on_line(_line):
            pass

        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app, scroll = screen.build_app(
                prompt="> ", history=None, completer=None, on_line=on_line,
                on_interrupt=lambda: None, on_eof=lambda: None,
            )
            ready, toggled = asyncio.Event(), asyncio.Event()
            original = scroll.toggle_reasoning

            def toggle():
                result = original()
                toggled.set()
                return result

            scroll.toggle_reasoning = toggle
            task = asyncio.create_task(app.run_async(pre_run=ready.set))
            try:
                await asyncio.wait_for(ready.wait(), 2)
                app.current_buffer.text = "unfinished input"
                block = scroll.reasoning(1)
                block.append(THOUGHT)
                block.answer_started()
                assert THOUGHT not in transcript.strip_ansi(scroll.buffer.text)
                for visible in (True, False):
                    toggled.clear()
                    pipe.send_bytes(b"\x12")
                    await asyncio.wait_for(toggled.wait(), 2)
                    assert (THOUGHT in transcript.strip_ansi(scroll.buffer.text)) is visible
                    assert app.current_buffer.text == "unfinished input"
            finally:
                if app.is_running:
                    app.exit()
                await task
    asyncio.run(drive())


def test_screen_reasoning_shares_stdout_ordering_without_losing_queued_input():
    async def drive():
        scroll = screen.Scrollback()
        app = SimpleNamespace(invalidate=lambda: None)
        loop = asyncio.get_running_loop()
        output = screen._ScreenStdout(loop, scroll, app, io.StringIO())
        display = screen.ScreenReasoningDisplay(loop, scroll, app, 2)
        output.write("before\n")
        display.append("first\n")
        output.write("queued input\n")
        display.append("second\n")
        display.answer_started()
        output.write("answer\n")
        display.finish(7)
        await asyncio.sleep(0)
        visible = transcript.strip_ansi(scroll.buffer.text)
        assert visible.startswith("before\n")
        assert "first\nsecond\nqueued input\nanswer\n" in visible
        scroll.toggle_reasoning()
        hidden = transcript.strip_ansi(scroll.buffer.text)
        assert "first\nsecond\n" not in hidden
        assert "queued input\nanswer\n" in hidden
        scroll.toggle_reasoning()
        assert transcript.strip_ansi(scroll.buffer.text) == visible
    asyncio.run(drive())


@pytest.mark.parametrize("level", [1, 2, 3])
def test_scrollback_eviction_discards_fold_handles_not_recent_output(monkeypatch, level):
    monkeypatch.setattr(screen, "SCROLLBACK_LINES", 8)
    scroll = screen.Scrollback()
    block = scroll.reasoning(level)
    block.append(THOUGHT)
    block.answer_started()
    tail = "".join(f"tail {i}\n" for i in range(12))
    scroll.append(tail)
    before = scroll.buffer.text
    block.finish(17)
    assert scroll.buffer.text == before
    assert scroll.toggle_reasoning() is False
    assert scroll.buffer.text == before
    assert scroll.buffer.document.line_count <= 8
    latest = scroll.reasoning(2)
    latest.append("latest thought\n")
    scroll.append("latest answer\n")
    scroll.toggle_reasoning()
    scroll.toggle_reasoning()
    visible = transcript.strip_ansi(scroll.buffer.text)
    assert "latest thought\nlatest answer\n" in visible
    assert scroll.buffer.document.line_count <= 8


def test_unlogged_reasoning_bypasses_nested_and_replaced_transcript_sinks():
    from js.reasoning_display import StderrReasoning

    visible, first, second = io.StringIO(), io.StringIO(), io.StringIO()
    sink = transcript.TranscriptLogSink([first], [])
    inner = transcript.TranscriptTee(visible, lambda: sink)
    outer = transcript.TranscriptTee(inner, lambda: sink)
    display = StderrReasoning(2, outer)
    display.append("first thought")
    sink = transcript.TranscriptLogSink([second], [])
    display.append("second thought")
    display.finish()
    assert "first thoughtsecond thought" in transcript.strip_ansi(visible.getvalue())
    assert first.getvalue() == second.getvalue() == ""


def test_http_reasoning_streams_on_screen_before_answer_and_survives_collapse(monkeypatch, tmp_path):
    cfg = config.from_env(extras=[
        "model.id=qwen-test", "provider.id=openai", "provider.base_url=http://local.test/v1",
        "provider.api_key=fixture", "model.max_output_tokens=64", "model.context_window=262144",
        "model.vision=off", "ui.reasoning=1",
    ])
    scroll = screen.Scrollback()
    app = SimpleNamespace(invalidate=lambda: None)
    received_before_answer = []

    def sse(delta, finish=None):
        chunk = {"id": "offline", "object": "chat.completion.chunk", "created": 1,
                 "model": "qwen-test", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(chunk)}\n\n".encode()

    class Body(httpx2.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield sse({"role": "assistant", "reasoning_content": THOUGHT})
            await asyncio.sleep(0)
            visible = transcript.strip_ansi(scroll.buffer.text)
            received_before_answer.append(THOUGHT in visible and "answer" not in visible)
            yield sse({"content": "answer"})
            yield sse({}, "stop") + b"data: [DONE]\n\n"

        async def aclose(self):
            self.closed = True

    body = Body()

    async def drive():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, stream=body),
        )) as client:
            get_provider = model_client.ai.get_provider
            monkeypatch.setattr(model_client.ai, "get_provider", lambda *a, **kw: get_provider(*a, **kw, client=client))
            loop = asyncio.get_running_loop()
            telemetry = runtime.Telemetry(None, reasoning_factory=lambda level: screen.ScreenReasoningDisplay(loop, scroll, app, level))
            messages = [{"role": "user", "content": "first"}]
            with screen.capture_stdio(loop, scroll, app):
                await runtime.run_turn_async(
                    cfg, "system", messages, telemetry, trace_override=False,
                    tool_context=ToolContext(cwd=tmp_path),
                )
            await asyncio.sleep(0)
            assert received_before_answer == [True]
            assert body.closed
            assert THOUGHT not in transcript.strip_ansi(scroll.buffer.text)
            assert "answer" in transcript.strip_ansi(scroll.buffer.text)
            memory.persist_messages(cfg.session_file, messages)
            assert memory.load_messages(cfg.session_file, preserve_reasoning=True)[-1]["reasoning_content"] == THOUGHT
            scroll.toggle_reasoning()
            assert THOUGHT in transcript.strip_ansi(scroll.buffer.text)
    asyncio.run(drive())


@pytest.mark.parametrize(("level", "cancel"), [(0, False), (2, False), (2, True)])
def test_prompt_mode_displays_reasoning_separately_and_persists_it(monkeypatch, tmp_path, capsys, level, cancel):
    prompts = tmp_path / ".js" / "agents" / "reason-ui"
    prompts.mkdir(parents=True)
    (prompts / "01-prompt.md").write_text("system\n")
    cfg = config.from_env(agent_id="reason-ui", extras=[
        "model.id=qwen-test", "provider.id=openai", "provider.base_url=http://local.test/v1",
        "provider.api_key=fixture", "model.max_output_tokens=64", "model.context_window=262144",
        "model.vision=off", "runtime.debug_autolog=off", f"ui.reasoning={level}",
    ])
    monkeypatch.setattr(cli, "_from_env", lambda *a, **kw: cfg)
    scripted(monkeypatch, cancel=cancel)
    assert cli._run_prompt("first") == (130 if cancel else 0)
    displayed = capsys.readouterr()
    assert (THOUGHT in transcript.strip_ansi(displayed.err)) is (level > 0)
    assert THOUGHT not in displayed.out
    messages = memory.load_messages(cfg.session_file, preserve_reasoning=True)
    assert messages[-1]["reasoning_content"] == THOUGHT
    if cancel:
        assert messages[-1]["incomplete_reason"] == "cancelled"
    else:
        assert messages[-1]["content"] == "answer"
        assert "answer" in displayed.out
    log_path = cli._transcript_log_path(cfg, cfg.settings)
    assert log_path is not None
    assert THOUGHT not in log_path.read_text()
