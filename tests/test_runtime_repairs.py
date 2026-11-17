"""Model-side mistakes the runtime repairs instead of failing the call:
lenient argument JSON, proxy-cloaked tool names, `_`-named extras, repeated
errors, benign shell exits and tools that cannot run."""

from __future__ import annotations

import json

import ai
import ai.types.messages
import ai.types.usage
from turn_driver import recording, registry as turn_registry, run

from js import runtime, tool_args
from js.config import Config
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import (
    Tool,
    ToolContext,
    ToolRegistry,
    build_default_registry,
    fs,
    python_kernel,
    shell_fetch,
    web_search,
)


def _tool(name: str, handler=None, params: dict | None = None) -> Tool:
    return Tool(name, f"{name} tool", handler or (lambda command="": f"{name} ran {command}"),
                params if params is not None else {"command": {"type": "string"}})


def _registry(*tools: Tool) -> ToolRegistry:
    return build_default_registry(suites=(("test", tools),), prompts_root=[])


def _config(tmp_path, settings: dict | None = None) -> Config:
    return Config(
        agent_id="test-agent",
        agent_dir=tmp_path / ".js" / "sessions" / "test-agent",
        model="offline-test-model",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=8,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / "test-agent",
        session_file=tmp_path / ".js" / "sessions" / "test-agent" / "runtime.jsonl",
        prompts_dir=tmp_path / "prompts",
        settings=settings or {},
    )


def _reply(calls: list[tuple[str, str, str]] | None = None, text: str = "") -> ModelStreamResult:
    tool_calls = [ModelToolCall(id=cid, name=name, arguments=args) for cid, name, args in calls or []]
    parts: list = [ai.types.messages.TextPart(text=text)] if text else []
    parts += [ai.types.messages.ToolCallPart(tool_call_id=c.id, tool_name=c.name, tool_args=c.arguments)
              for c in tool_calls]
    return ModelStreamResult(
        text=text,
        tool_calls=tool_calls,
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=5, output_tokens=5),
        finish_reason="tool_calls" if tool_calls else "stop",
        assistant_message=ai.types.messages.Message(
            role="assistant", parts=parts or [ai.types.messages.TextPart(text="")]),
    )


def _run(monkeypatch, tmp_path, registry, replies, *, telemetry=None, seen=None, settings=None):
    replies = iter(replies)

    def stream_stub(**kwargs):
        if seen is not None:
            seen.append(kwargs)
        return next(replies)

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream_stub)
    messages = [{"role": "user", "content": "go"}]
    runtime.run_turn(
        _config(tmp_path, settings), "system", messages, telemetry or runtime.Telemetry(None),
        trace_override=False, tool_context=ToolContext(cwd=tmp_path), suppress_output=True,
        tool_registry=registry,
    )
    return messages


def _flight(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- lenient JSON ------------------------------------------------------------


def test_raw_newlines_and_tabs_inside_argument_strings_parse():
    assert tool_args.repair_jsonish('{"content": "a\nb\tc"}') == {"content": "a\nb\tc"}


def test_stringified_edits_with_raw_newlines_reach_patch(monkeypatch, tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    fs.read("f.txt", context=context)
    patch = next(tool for tool in fs.tools() if tool.name == "patch")
    # The inner JSON carries literal newlines inside its string values.
    edits = '[{"old_string": "one\ntwo", "new_string": "ONE\nTWO"}]'
    arguments = json.dumps({"file_path": "f.txt", "edits": edits})

    turn = run(tmp_path, monkeypatch, turn_registry(patch), [[("c1", "patch", arguments)]], context=context)

    assert [e["invalid"] for e in turn.events("tool_call_batch_normalized")] == [0]
    assert not turn.results()["c1"].startswith("ERROR"), turn.results()["c1"]
    assert target.read_text(encoding="utf-8") == "ONE\nTWO\n"


# --- proxy-cloaked tool names --------------------------------------------------


def test_cloaked_names_resolve_to_the_one_published_tool():
    registry = _registry(_tool("shell"), _tool("fs_search"), _tool("read"))
    for cloaked, tool in [
        ("promote_shell", "shell"),
        ("promote_shell_placeholder", "shell"),
        ("deer_read", "read"),
        ("arena_fs_search", "fs_search"),
        ("mcp__exile_poem__fs_search_placeholder_never_called", "fs_search"),
        ("mcp__exile_poem__crush_shell", "shell"),
    ]:
        assert registry.repaired_name(cloaked) == tool, cloaked
    assert registry.repaired_name("mcp__cruel_lawn__check") == ""


def test_a_cloaked_name_matching_two_tools_is_not_repaired():
    registry = _registry(_tool("fs_search"), _tool("search"))
    assert registry.repaired_name("mcp__exile_poem__fs_search") == ""


def test_promote_shell_dispatches_shell_and_records_the_repair(monkeypatch, tmp_path):
    registry = _registry(_tool("shell"))
    flight = tmp_path / "flight.jsonl"
    messages = _run(monkeypatch, tmp_path, registry, [
        _reply([("c1", "promote_shell", '{"command": "echo hi"}')]),
        _reply(text="done"),
    ], telemetry=runtime.Telemetry(flight))

    call = next(m for m in messages if m.get("tool_calls"))["tool_calls"][0]
    assert call["function"]["name"] == "shell"
    result = next(m for m in messages if m.get("role") == "tool")
    assert result["content"].startswith("shell ran echo hi")
    repaired = [e for e in _flight(flight) if e["kind"] == "tool_name_repaired"]
    assert [(e["received"], e["tool"]) for e in repaired] == [("promote_shell", "shell")]


def test_a_cloaked_name_never_reaches_an_unpublished_tool(monkeypatch, tmp_path):
    # shell is allowed but lazy and not loaded, so it is not on the surface.
    allowed = build_default_registry().select(["shell:lazy", "read:eager"])
    surface = allowed.lazy_surface(tmp_path)
    frozen = surface.dispatch_registry()
    assert frozen.resolve("shell") is None
    assert frozen.repaired_name("promote_shell") == ""

    marker = tmp_path / "ran"
    turn = run(tmp_path, monkeypatch, allowed,
               [[("c1", "promote_shell", json.dumps({"command": f"touch {marker}"}))]])
    assert turn.events("tool_name_repaired") == []
    assert [e["tool"] for e in turn.events("tool_unknown")] == ["promote_shell"]
    assert turn.results()["c1"].startswith("ERROR")
    assert not marker.exists()

    only_read = _registry(_tool("read"))
    assert only_read.repaired_name("promote_shell") == ""


# --- `_`-named extras ----------------------------------------------------------


def test_undeclared_underscore_properties_are_dropped_before_validation(monkeypatch, tmp_path):
    tool, received = recording("tasks", params={"name": {"type": "string"}})

    turn = run(tmp_path, monkeypatch, turn_registry(tool),
               [[("c1", "tasks", '{"name": "a", "_message": "hi"}')]])

    assert [e["invalid"] for e in turn.events("tool_call_batch_normalized")] == [0]
    assert received == [{"name": "a"}]
    assert [json.loads(args) for _id, _name, args in turn.calls()] == [{"name": "a"}]


# --- repeated errors -----------------------------------------------------------


def test_numbers_paths_and_quotes_do_not_make_errors_different():
    tracker = runtime.ToolErrorTracker(limit=2)
    tracker.record("read", "ERROR: /a/b.txt not found at line 3 ('x')")
    tracker.record("read", "ERROR: /c/d.py not found at line 90 ('yy')")
    assert tracker.steer_due() == [("read", 2)]


def test_a_different_error_or_a_success_starts_the_count_over():
    tracker = runtime.ToolErrorTracker(limit=2)
    tracker.record("read", "ERROR: no such file")
    tracker.record("read", "ERROR: permission denied")
    assert tracker.steer_due() == []
    tracker.record("read", "ERROR: permission denied")
    assert tracker.steer_due() == [("read", 2)]
    assert tracker.steer_due() == []
    assert not tracker.limit_reached()
    tracker.record("read", "ok")
    tracker.record("read", "ERROR: permission denied")
    assert tracker.steer_due() == []
    assert not tracker.limit_reached()


def test_the_same_error_after_the_steer_ends_the_turn():
    tracker = runtime.ToolErrorTracker(limit=2)
    tracker.record("read", "ERROR: no such file")
    tracker.record("read", "ERROR: no such file")
    assert tracker.steer_due() == [("read", 2)]
    assert not tracker.limit_reached()
    tracker.record("read", "ERROR: no such file")
    assert tracker.exhausted() == "read"


def test_varied_errors_do_not_end_the_turn(monkeypatch, tmp_path):
    errors = iter(["ERROR: no such mode", "ERROR: permission denied", "ERROR: the index is locked",
                   "ERROR: no such mode", "ERROR: permission denied"])
    registry = _registry(_tool("flaky", handler=lambda **kwargs: next(errors)))
    replies = [_reply([(f"c{n}", "flaky", "{}")]) for n in range(5)] + [_reply(text="done")]
    messages = _run(monkeypatch, tmp_path, registry, replies)

    assert messages[-1]["content"] == "done"
    assert not any(m.get("steered") for m in messages)


def test_a_repeated_error_steers_once_then_the_loop_continues(monkeypatch, tmp_path):
    outcomes = iter(["ERROR: deliberate failure"] * 3 + ["fine"])
    registry = _registry(_tool("flaky", handler=lambda **kwargs: next(outcomes)))
    replies = [_reply([(f"c{n}", "flaky", "{}")]) for n in range(4)] + [_reply(text="done")]
    messages = _run(monkeypatch, tmp_path, registry, replies)

    assert messages[-1]["content"] == "done"
    steers = [index for index, m in enumerate(messages) if m.get("steered")]
    assert len(steers) == 1
    # The steer follows the third failed result and comes before the next call.
    assert messages[steers[0] - 1]["role"] == "tool"
    assert sum(1 for m in messages[:steers[0]] if m.get("role") == "tool") == 3


# --- benign shell exits --------------------------------------------------------


def test_sigpipe_from_an_early_reader_is_noted():
    for command in ["yes | head -n 1", "cat big | tail -5", "yes | grep -m1 y", "yes | grep -q y",
                    "yes | sed q", "seq 100 | sed -n '1,5p;5q'", "yes | /usr/bin/head -c 10 2>&1"]:
        assert shell_fetch._exit_note(command, 141, 10), command
    assert shell_fetch._exit_note("yes | sort", 141, 10) == ""
    assert shell_fetch._exit_note("yes | grep y", 141, 10) == ""
    assert shell_fetch._exit_note("yes | head -n 1", 1, 10) == ""


def test_shell_result_notes_sigpipe_and_keeps_the_exit(tmp_path):
    command = "yes | head -n 1"
    result = str(shell_fetch.shell(command, context=ToolContext(cwd=tmp_path)))
    assert "\nexit=141\n" in result
    assert shell_fetch._exit_note(command, 141, 2) in result


def test_grep_with_no_match_is_noted():
    assert shell_fetch._exit_note("cat f | grep zzz", 1, 0)
    assert shell_fetch._exit_note("rg zzz src", 1, 0)
    assert shell_fetch._exit_note("grep zzz f", 1, 4) == ""
    assert shell_fetch._exit_note("grep zzz f", 2, 0) == ""
    assert shell_fetch._exit_note("false", 1, 0) == ""


def test_shell_result_notes_grep_no_match(tmp_path):
    command = "printf 'a\\n' | grep zzz"
    result = str(shell_fetch.shell(command, context=ToolContext(cwd=tmp_path)))
    assert "\nexit=1\n" in result
    assert shell_fetch._exit_note(command, 1, 0) in result


# --- tools that cannot run -----------------------------------------------------


def test_kernel_is_left_out_when_its_dependencies_are_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(python_kernel, "missing_dependencies", lambda: "ERROR: kernel needs jupyter_client")
    registry = build_default_registry(suites=(("kernel", python_kernel.tools()), ("test", (_tool("read"),))),
                                      prompts_root=[])
    runnable, left_out = registry.runnable(ToolContext(cwd=tmp_path))
    assert set(left_out) == {"kernel"}
    assert runnable.resolve("kernel") is None
    assert runnable.unavailable_error("kernel") == "ERROR: kernel needs jupyter_client"

    seen: list[dict] = []
    flight = tmp_path / "flight.jsonl"
    _run(monkeypatch, tmp_path, registry, [_reply(text="done")], seen=seen,
         telemetry=runtime.Telemetry(flight))
    assert "kernel" not in {tool.name for tool in seen[0]["tools"]}
    assert "read" in {tool.name for tool in seen[0]["tools"]}
    assert [e["tool"] for e in _flight(flight) if e["kind"] == "tool_unpublished"] == ["kernel"]


def test_kernel_stays_when_its_dependencies_are_present(monkeypatch, tmp_path):
    monkeypatch.setattr(python_kernel, "missing_dependencies", lambda: "")
    registry = build_default_registry(suites=(("kernel", python_kernel.tools()),), prompts_root=[])
    runnable, left_out = registry.runnable(ToolContext(cwd=tmp_path))
    assert left_out == {}
    assert runnable.resolve("kernel") is not None


def test_search_tools_without_their_api_key_are_left_out(monkeypatch, tmp_path):
    for setting in web_search.API_KEY_SETTINGS.values():
        monkeypatch.delenv(setting, raising=False)
    registry = build_default_registry(suites=(("search", web_search.tools()),), prompts_root=[])
    context = ToolContext(cwd=tmp_path)
    context.tavily_api_key = "key"
    runnable, left_out = registry.runnable(context)
    assert set(left_out) == {"serper_search", "exa_search"}
    assert runnable.resolve("tavily_search") is not None
    assert runnable.resolve("serper_search") is None
    assert "SERPER_API_KEY" in runnable.unavailable_error("serper_search")
