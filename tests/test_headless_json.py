"""`js -p --json`: one JSON event per line on stdout (js-1g1.18).

The schema these tests pin is in docs/headless-json.md."""

from __future__ import annotations

import json

import ai
import ai.types.usage
import pytest

from js import cli, runtime, usage
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit.core import ToolContext


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))
    monkeypatch.setattr(usage, "_TOTALS", {})

    class Terminal:
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stdin", Terminal())


def _reply(text="", calls=(), use=(10, 5)):
    tool_calls = [ModelToolCall(id=call_id, name=name, arguments=args) for call_id, name, args in calls]
    parts: list = [ai.types.messages.ToolCallPart(tool_call_id=c.id, tool_name=c.name, tool_args=c.arguments)
                   for c in tool_calls]
    if text or not parts:
        parts.append(ai.types.messages.TextPart(text=text))
    return ModelStreamResult(
        text=text, tool_calls=tool_calls, reasoning="",
        usage=ai.types.usage.Usage(input_tokens=use[0], output_tokens=use[1]),
        finish_reason="tool_calls" if tool_calls else "stop",
        assistant_message=ai.messages.Message(role="assistant", parts=parts),
    )


def _stub(monkeypatch, replies):
    """Each reply streams its text in two chunks before it returns."""
    replies = iter(replies)

    def stream(**kw):
        reply = next(replies)
        if isinstance(reply, BaseException):
            raise reply
        half = len(reply.text) // 2
        for chunk in (reply.text[:half], reply.text[half:]):
            if chunk:
                kw["on_text"](chunk)
        return reply

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)


def _events(out: str) -> list[dict]:
    lines = out.splitlines()
    assert lines, "no events"
    return [json.loads(line) for line in lines]  # every stdout line is JSON


def _run(monkeypatch, capsys, replies, argv):
    _stub(monkeypatch, replies)
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, _events(captured.out), captured.err


def test_a_turn_with_a_tool_call_streams_every_event_kind(monkeypatch, capsys, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    code, events, _err = _run(monkeypatch, capsys, [
        _reply("Let me read it.", calls=[("c1", "read", json.dumps({"file_path": str(target)}))], use=(100, 20)),
        _reply("It says alpha.", use=(300, 30)),
    ], ["-p", "what is in notes.txt", "--json"])

    assert code == 0
    kinds = [e["type"] for e in events]
    assert kinds[0] == "session"
    assert kinds[-1] == "result"
    for kind in ("turn_start", "text", "message", "tool_call", "tool_result", "usage", "turn_end"):
        assert kind in kinds

    session = events[0]
    assert session["resumed"] is False
    assert session["file"].endswith(".jsonl")

    deltas = "".join(e["delta"] for e in events if e["type"] == "text")
    assert deltas == "Let me read it.It says alpha."
    assert [e["text"] for e in events if e["type"] == "message"] == ["Let me read it.", "It says alpha."]

    call = next(e for e in events if e["type"] == "tool_call")
    assert (call["id"], call["name"], call["arguments"]) == ("c1", "read", {"file_path": str(target)})
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["id"] == "c1" and result["name"] == "read"
    assert result["ok"] is True and result["bytes"] > 0 and isinstance(result["summary"], str)
    assert kinds.index("tool_call") < kinds.index("tool_result")

    usages = [e for e in events if e["type"] == "usage"]
    assert [(u["input_tokens"], u["output_tokens"]) for u in usages] == [(100, 20), (300, 30)]
    assert usages[-1]["session"]["input_tokens"] == 400

    end = next(e for e in events if e["type"] == "turn_end")
    assert end["reason"] == "stop"
    assert end["usage"]["calls"] == 2 and end["usage"]["output_tokens"] == 50

    final = events[-1]
    assert final["ok"] is True and final["exit_code"] == 0
    assert final["text"] == "It says alpha."
    assert final["session"] == session["id"]
    assert final["usage"]["calls"] == 2


def test_stdout_carries_only_json_even_with_the_debug_trace_on(monkeypatch, capsys):
    code, events, err = _run(monkeypatch, capsys, [_reply("plain answer")], ["-p", "hi", "--json", "--debug"])

    assert code == 0
    assert all(isinstance(e, dict) and "type" in e for e in events)
    assert "plain answer" in err  # --debug streams the answer; it lands on stderr


def test_a_resumed_session_says_so_and_carries_its_totals(monkeypatch, capsys):
    code, first, _ = _run(monkeypatch, capsys, [_reply("one", use=(7, 3))], ["-p", "hi", "--json"])
    assert code == 0
    session_id = first[0]["id"]
    usage.forget(first[0]["file"])  # the next run is a new process

    code, second, _ = _run(monkeypatch, capsys, [_reply("two", use=(11, 4))],
                           ["-p", "again", "--json", "--session", session_id])

    assert code == 0
    assert second[0]["resumed"] is True
    assert second[0]["usage"]["input_tokens"] == 7
    assert second[-1]["usage"]["input_tokens"] == 18


def test_a_provider_error_is_an_error_event_and_a_failed_result(monkeypatch, capsys):
    failure = ai.ProviderAPIError("bad request", is_retryable=False)
    code, events, _err = _run(monkeypatch, capsys, [failure], ["-p", "hi", "--json"])

    assert code != 0
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "bad request" in errors[0]["message"]
    assert errors[0]["retryable"] is False
    assert any(e["type"] == "turn_end" and e["reason"] == "error" for e in events)
    assert events[-1]["type"] == "result" and events[-1]["ok"] is False


def test_a_failure_before_the_turn_still_ends_with_error_and_result(monkeypatch, capsys):
    code, events, _err = _run(monkeypatch, capsys, [], ["-p", "hi", "--json", "--reasoning", "loud"])

    assert code == 2
    assert [e["type"] for e in events] == ["error", "result"]
    assert events[0]["message"]
    assert events[1]["exit_code"] == 2


def test_json_needs_a_headless_run(capsys):
    assert cli.main(["--json"]) == 2
    assert capsys.readouterr().out == ""


def test_the_transcript_log_keeps_the_answer(monkeypatch, capsys, tmp_path):
    code, _events_, _err = _run(monkeypatch, capsys, [_reply("JSON-ANSWER")], ["-p", "hi-json", "--json"])

    assert code == 0
    logs = [p.read_text(encoding="utf-8") for p in (tmp_path / ".js").rglob("*.log")]
    log = next(text for text in logs if "hi-json" in text)
    assert "JSON-ANSWER" in log


@pytest.mark.parametrize("extra", [
    ["--debug", "--debug-file", "trace.log"],
    ["-C", "/nonexistent/dir/for/json"],
])
def test_a_command_line_refused_before_the_run_still_ends_with_error_and_result(monkeypatch, capsys, extra):
    code, events, err = _run(monkeypatch, capsys, [], ["-p", "hi", "--json", *extra])

    assert code == 2
    assert [e["type"] for e in events] == ["error", "result"]
    assert events[0]["message"] and not events[0]["message"].startswith("*")
    assert events[1]["exit_code"] == 2 and events[1]["ok"] is False
    assert err.strip()


def test_piped_stdin_with_json_is_a_headless_run(monkeypatch, capsys):
    class Pipe:
        def isatty(self):
            return False

        def read(self):
            return "piped question"

    monkeypatch.setattr(cli.sys, "stdin", Pipe())
    code, events, _err = _run(monkeypatch, capsys, [_reply("piped answer")], ["--json"])

    assert code == 0
    assert [e["type"] for e in events][0] == "session"
    assert events[-1]["type"] == "result" and events[-1]["text"] == "piped answer"


def _progress_lines(err: str) -> list[str]:
    from js import messages as msgs

    lead = msgs.PROGRESS_MODEL.template.split("{")[0]
    return [line for line in err.splitlines() if lead in line]


def _progress(interval=30):
    import io
    import time

    from js import headless
    from js.toolkit.core import TurnStatus

    out = io.StringIO()
    progress = headless.Progress(TurnStatus(), interval, out=out)
    return progress, out, time.monotonic()


def test_progress_waits_for_an_interval_of_silence_then_names_what_runs():
    from js import activity

    progress, out, start = _progress()
    with activity.model_call():
        progress.tick(start + 10)
        assert not out.getvalue()
        progress.tick(start + 31)
    assert out.getvalue()


def test_silence_counts_across_model_calls_and_tools_that_each_end_quickly():
    from js import activity

    progress, out, start = _progress()
    with activity.model_call():
        progress.tick(start + 10)
    with activity.tool_call("shell"):
        progress.tick(start + 20)
    with activity.model_call():
        progress.tick(start + 31)

    assert out.getvalue()


def test_progress_names_the_tool_running_now_not_one_that_finished():
    from js import activity

    progress, _out, _start = _progress()
    with activity.tool_call("read"):
        pass
    with activity.tool_call("shell"):
        line = progress.line()

    assert "shell" in line and "read" not in line


def test_a_wait_before_a_model_call_is_sent_again_is_progress():
    from js import activity

    progress, _out, _start = _progress()
    with activity.retry_wait(90):
        line = progress.line()

    assert line is not None and "90" in line


def _record_retry_waits(monkeypatch) -> list[str]:
    from js import activity, retry

    seen: list[str] = []

    async def sleep(_delay):
        seen.extend(item.kind for item in activity.running())

    monkeypatch.setattr(retry, "_sleep", sleep)
    return seen


def test_retry_holds_its_wait_as_activity(monkeypatch):
    import asyncio

    import ai

    from js import activity, retry

    seen = _record_retry_waits(monkeypatch)
    attempts = iter([ai.ProviderAPIError("busy", is_retryable=True), None])

    async def request():
        failure = next(attempts)
        if failure is not None:
            raise failure
        return "ok"

    asyncio.run(retry.call(request, retry.Budget(attempts=2, max_wait=0)))

    assert activity.RETRY in seen


def test_a_one_shot_turns_retry_wait_is_activity(monkeypatch):
    import ai

    from js import activity

    seen = _record_retry_waits(monkeypatch)
    replies = iter([ai.ProviderAPIError("busy", is_retryable=True), _reply("done")])

    def stream(**kw):
        reply = next(replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)

    assert cli.main(["-p", "hi", "--no-save"]) == 0
    assert activity.RETRY in seen


def test_a_write_to_watched_stdout_or_stderr_starts_the_silence_over(monkeypatch):
    import io
    import sys
    import time

    from js import activity, headless

    for name in ("stdout", "stderr"):
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        progress, out, _start = _progress()
        with headless.watch_output(), activity.model_call():
            time.sleep(0.2)
            print("trace line", file=getattr(sys, name))
            progress.tick(time.monotonic() + 29.9)
        assert not out.getvalue(), name


def test_a_trace_line_between_calls_keeps_a_traced_one_shot_quiet(monkeypatch, capsys):
    """Two model calls that together outlast the interval, with the tool's
    trace line between them: the trace is a sign of life."""

    monkeypatch.setenv("JS_PROGRESS_INTERVAL", "0.5")
    monkeypatch.setenv("JS_TRACE", "1")
    import asyncio

    replies = iter([_reply("", calls=[("c1", "shell", json.dumps({"command": "echo hi"}))]), _reply("done")])

    async def stream(model, **kw):
        await asyncio.sleep(0.35)
        return next(replies)

    monkeypatch.setattr(runtime.model_client, "resolve_model", lambda *a, **k: type("M", (), {"provider": _Provider()})())
    monkeypatch.setattr(runtime.model_client, "_stream_with_model", stream)

    assert cli.main(["-p", "hi", "--no-save"]) == 0
    assert not _progress_lines(capsys.readouterr().err)


def test_a_dispatched_tool_call_is_activity_while_it_runs(tmp_path):
    from js import activity
    from js.toolkit.core import Tool, ToolContext
    from js.toolkit.registry import ToolRegistry

    seen = []

    def probe(context=None):
        seen.extend((item.kind, item.name) for item in activity.running())
        return "ok"

    registry = ToolRegistry((Tool("probe", "test", probe, {}),), {})
    runtime._dispatch("probe", "{}", runtime.Telemetry(debug_log=None), 10_000, registry=registry,
                      tool_context=ToolContext(cwd=tmp_path))

    assert (activity.TOOL, "probe") in seen


def test_hidden_reasoning_does_not_count_as_a_sign_of_life():
    """Tokens that reach no stream leave the run looking hung."""
    from js import activity

    progress, out, start = _progress()
    with activity.model_call():
        progress.status.stream("hidden reasoning")
        progress.tick(start + 31)

    assert out.getvalue()


class _Provider:
    async def aclose(self):
        return None


def _slow_model(monkeypatch, *, slow_calls=(1,)):
    """The real stream_model_async over a stubbed provider: call N sleeps
    when N is in `slow_calls`."""
    import asyncio

    calls = []

    async def stream(model, **kw):
        calls.append(kw)
        if len(calls) in slow_calls:
            await asyncio.sleep(0.4)
        return _reply("done")

    monkeypatch.setattr(runtime.model_client, "resolve_model", lambda *a, **k: type("M", (), {"provider": _Provider()})())
    monkeypatch.setattr(runtime.model_client, "_stream_with_model", stream)


def test_any_model_call_counts_as_activity_as_a_compaction_summary_does(monkeypatch):
    """Compaction and subagents call the model outside the turn's own call."""
    import io

    from js import headless
    from js.toolkit.core import TurnStatus

    _slow_model(monkeypatch)
    out = io.StringIO()
    with headless.Progress(TurnStatus(), 0.05, out=out):
        runtime.model_client.run_owning_loop(runtime.model_client.stream_model_async(
            model_id="m", provider_id="p", provider_base_url=None, provider_api_key=None,
            messages=[], tools=None, max_output_tokens=None, reasoning_effort=None, on_text=lambda _t: None))

    assert out.getvalue()


def test_a_slow_one_shot_model_call_writes_progress_to_stderr(monkeypatch, capsys):
    monkeypatch.setenv("JS_PROGRESS_INTERVAL", "0.05")
    _slow_model(monkeypatch)

    code = cli.main(["-p", "hi", "--no-save"])

    assert code == 0
    assert _progress_lines(capsys.readouterr().err)


def test_a_slow_startup_exchange_writes_progress_to_stderr(monkeypatch, capsys, tmp_path):
    agent = tmp_path / ".js" / "agents" / "opener"
    agent.mkdir(parents=True)
    (agent / "01-prompt.md").write_text("You answer.", encoding="utf-8")
    (agent / "01-user.md").write_text("opening", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JS_PROGRESS_INTERVAL", "0.05")
    _slow_model(monkeypatch)

    code = cli.main(["-p", "hi", "--no-save", "--agent", "opener"])

    assert code == 0
    assert _progress_lines(capsys.readouterr().err)
