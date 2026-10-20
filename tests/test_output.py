from __future__ import annotations

from js.output import OutputEvent, StdoutSink

import pytest


def test_output_event_is_genuinely_hashable():
    """frozen=True advertises hashability; a dict `fields` field used to make
    hash() raise despite that. `fields` is now a tuple of (key, value) pairs,
    so hash() must work, e.g. for a view that dedups events in a set."""
    event = OutputEvent(name="turn_start", fields=(("agent_id", "worker-1"),))

    assert hash(event) == hash(event)
    assert event in {event}


def test_output_event_fields_round_trip_through_dict():
    event = OutputEvent(name="tool_call", fields=(("name", "read"), ("ok", True)))

    assert dict(event.fields) == {"name": "read", "ok": True}


def test_runtime_events_with_structured_payloads_are_hashable():
    from js.output import event

    one = event("tool_result", args={"path": "note.txt"}, view={"lines": ["alpha"]})
    same = event("tool_result", args={"path": "note.txt"}, view={"lines": ["alpha"]})
    other = event("tool_result", args={"path": "note.txt"}, view={"lines": ["beta"]})

    assert {one, same, other} == {one, other}


def test_stdout_sink_prints_text_as_a_line(capsys):
    StdoutSink().emit(OutputEvent(name="turn_end", text="done"))

    assert capsys.readouterr().out == "done\n"


def test_stdout_sink_streams_text_without_trailing_newline(capsys):
    StdoutSink().emit(OutputEvent(name="stream", text="chunk"))

    assert capsys.readouterr().out == "chunk"


def test_replacing_telemetry_builds_sinks_for_the_replacement():
    from dataclasses import replace
    from types import SimpleNamespace

    from js import output, runtime

    old, new = [], []

    def factory(chunks):
        return lambda markdown: SimpleNamespace(
            speaker=lambda *args: None, chunk=lambda kind, text: chunks.append(text))

    original = runtime.Telemetry(None, display_factory=factory(old))
    output.sink_of(original)
    copied = replace(original, display_factory=factory(new))
    stream = output.Key()
    sink = output.sink_of(copied)
    sink.emit(output.event("stream_open", stream=stream))
    sink.emit(output.event("answer", stream=stream, text="answer", markdown=False, suppressed=False))

    assert old == []
    assert new == ["answer"]


@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("together", [False, True])
def test_parallel_calls_emit_paired_events_even_without_trace(tmp_path, trace, together, capsys):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from js import runtime
    from js.toolkit.core import Tool, ToolContext
    from js.toolkit.registry import ToolRegistry

    barrier = threading.Barrier(2)

    def probe(label):
        barrier.wait(timeout=5)
        return label

    registry = ToolRegistry((Tool("probe", "test", probe, {"label": {"type": "string"}}),), {})
    context = ToolContext(cwd=tmp_path)
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 0})
    sink = _Sink()
    telemetry = runtime.Telemetry(None, sink=sink)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runtime._dispatch, "probe", '{"label":"' + label + '"}',
                               telemetry, 1000, trace, registry=registry,
                               tool_context=context, trace_together=together) for label in ("a", "b")]
        for future in futures:
            future.result()

    calls, results = sink.named("tool_call"), sink.named("tool_result")
    assert len(calls) == len(results) == 2
    for result in results:
        call = next(call for call in calls if call["call"] is result["call"])
        assert call["args"]["label"] == result["result"]
        assert sink.events.index(next(e for e in sink.events if e.name == "tool_call" and
                                     dict(e.fields)["call"] is call["call"])) < sink.events.index(
            next(e for e in sink.events if e.name == "tool_result" and
                 dict(e.fields)["call"] is call["call"]))
    assert capsys.readouterr() == ("", "")


def test_custom_sink_receives_retry_and_network_lines_without_using_the_screen(monkeypatch, tmp_path, capsys):
    from js import retry, runtime, stream_transport

    screen = []
    monkeypatch.setattr(stream_transport, "_sink", stream_transport.NetSink(lambda: 3, screen.append))

    def respond(**kwargs):
        retry.announce(1, retry.Budget.from_settings({}), 10, ValueError("offline failure"))
        stream_transport.say_or_print(1, "offline diagnostic")
        kwargs["on_text"]("answer")
        return _reply("answer")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", respond)
    sink = _Sink()
    _run(_offline_config(tmp_path, trace=True, settings={}), tmp_path, sink, "hello")

    assert screen == []
    assert len(sink.named("network")) == 1
    assert len(sink.named("retry_wait")) == 1
    assert capsys.readouterr() == ("", "")


def test_fanout_sinks_receive_parallel_events_in_the_same_order():
    import threading
    from concurrent.futures import ThreadPoolExecutor, wait

    from js import output

    entered, release = threading.Event(), threading.Event()
    first, second = _Sink(), _Sink()

    class PauseFirst:
        def emit(self, event):
            first.emit(event)
            if event.name == "first":
                entered.set()
                assert release.wait(timeout=5)

    fanout = output.Fanout(PauseFirst(), second)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(fanout.emit, OutputEvent("first"))
        assert entered.wait(timeout=5)
        two = pool.submit(fanout.emit, OutputEvent("second"))
        wait([two], timeout=0.1)
        release.set()
        one.result()
        two.result()

    assert [e.name for e in first.events] == [e.name for e in second.events]


def _offline_config(tmp_path, *, trace: bool, settings: dict):
    from js.config import Config

    home = tmp_path / ".js" / "sessions" / "a"
    return Config(agent_id="a", agent_dir=home, model="offline-model", provider_id=None,
                  provider_base_url=None, provider_api_key=None, reasoning_effort=None,
                  max_output_tokens=None, max_tool_iterations=6, max_bash_output_bytes=65536,
                  max_tool_result_bytes=65536, fetch_timeout_s=5, debug_log=None, trace=trace,
                  history_file=tmp_path / ".history", sessions_dir=home, session_file=home / "s.jsonl",
                  prompts_dir=tmp_path / "prompts", settings=settings)


def _reply(text="", tool_calls=(), reasoning=""):
    import ai.types.usage

    from js.model_client import ModelStreamResult
    from test_runtime_offline_integration import _make_assistant_msg

    return ModelStreamResult(text=text, tool_calls=list(tool_calls), reasoning=reasoning,
                             usage=ai.types.usage.Usage(input_tokens=5, output_tokens=7),
                             finish_reason="tool_calls" if tool_calls else "stop",
                             assistant_message=_make_assistant_msg(text, list(tool_calls), reasoning))


class _Sink:
    def __init__(self):
        self.events: list[OutputEvent] = []

    def emit(self, event):
        self.events.append(event)

    def named(self, name):
        return [dict(event.fields) for event in self.events if event.name == name]


def _run(cfg, tmp_path, sink, prompt):
    import asyncio

    from js import runtime
    from js.toolkit import ToolContext

    messages = [{"role": "user", "content": prompt}]
    asyncio.run(runtime.run_turn_async(cfg, "system", messages, runtime.Telemetry(None, sink=sink),
                                       tool_context=ToolContext(cwd=tmp_path)))
    return messages


def test_a_turn_with_its_own_sink_gets_every_visible_thing_as_fields_and_prints_nothing(
        monkeypatch, tmp_path, capsys):
    from tool_loading import after_loading

    from js import runtime
    from js.model_client import ModelToolCall

    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    calls: list[dict] = []

    def respond(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            kwargs["on_reasoning"]("look first")
            return _reply(tool_calls=[ModelToolCall(id="r1", name="read", arguments='{"file_path":"notes.txt"}')],
                          reasoning="look first")
        kwargs["on_text"]("done")
        return _reply("done")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", after_loading(respond, "read"))
    sink = _Sink()
    _run(_offline_config(tmp_path, trace=True, settings={"REASONING_DISPLAY": 2}), tmp_path, sink, "read notes.txt")

    shown = capsys.readouterr()
    assert (shown.out, shown.err) == ("", "")
    assert sink.named("run_line")[0]["fields"][0] == "model=offline-model"
    started = [event for event in sink.named("tool_call") if event["name"] == "read"]
    finished = [event for event in sink.named("tool_result") if event["name"] == "read"]
    assert [event["args"] for event in started] == [{"file_path": "notes.txt"}]
    assert finished[0]["call"] is started[0]["call"]
    assert "alpha" in finished[0]["result"]
    assert [event["text"] for event in sink.named("reasoning")] == ["look first"]
    assert [event["text"] for event in sink.named("answer")] == ["done"]
    assert sink.named("call_stats")[-1]["finish"] == "stop"


def test_file_and_shell_tools_hand_the_screen_their_result_as_fields(monkeypatch, tmp_path):
    import json

    from tool_loading import after_loading

    from js import runtime
    from js.model_client import ModelToolCall

    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    calls: list[dict] = []

    def respond(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _reply(tool_calls=[
                ModelToolCall(id="r1", name="read", arguments='{"file_path":"notes.txt"}'),
                ModelToolCall(id="s1", name="shell", arguments='{"command":"echo out; echo err >&2; exit 3"}'),
            ])
        if len(calls) == 2:
            return _reply(tool_calls=[
                ModelToolCall(id="p1", name="patch", arguments=json.dumps(
                    {"file_path": "notes.txt", "old_string": "beta", "new_string": "gamma"})),
                ModelToolCall(id="w1", name="write", arguments=json.dumps(
                    {"file_path": "new.txt", "content": "x\n"})),
            ])
        return _reply("done")

    monkeypatch.setattr(runtime.model_client, "stream_model_async",
                        after_loading(respond, "read", "shell", "patch", "write"))
    sink = _Sink()
    _run(_offline_config(tmp_path, trace=True, settings={}), tmp_path, sink, "work")

    views = {event["name"]: event["view"] for event in sink.named("tool_result") if event["view"]}
    notes, new = str(tmp_path / "notes.txt"), str(tmp_path / "new.txt")
    assert views["read"] == {"path": notes, "start_line": 1, "end_line": 2, "total_lines": 2,
                             "lines": ["alpha", "beta"]}
    assert (views["shell"]["exit_code"], views["shell"]["stdout"], views["shell"]["stderr"]) == (
        3, "out\n", "err\n")
    assert (views["patch"]["path"], views["patch"]["before"], views["patch"]["after"]) == (
        notes, "alpha\nbeta\n", "alpha\ngamma\n")
    assert (views["write"]["path"], views["write"]["before"], views["write"]["after"]) == (new, None, "x\n")
