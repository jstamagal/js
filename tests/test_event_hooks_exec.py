"""`on EVENT exec CMD`: the command's stdout reaches the model once, as a
js-reminder on the next user message; a tool_call handler that exits 2 refuses
the call with one ERROR line; session_start, session_end, pre_compact and
post_compact fire where they say."""

from __future__ import annotations

import contextlib
import json

import pytest

from js import attach, cli, compaction, events, runtime, settings
from js.memory import load_messages
from js.toolkit import Tool, ToolContext, ToolRegistry
from test_runtime_offline_integration import model_text_result, model_tool_call_result, offline_config

SESSION = "hooks"


@pytest.fixture(autouse=True)
def plain_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")


def _state(tmp_path) -> tuple[dict, object]:
    cfg = offline_config(tmp_path)
    state = {
        "messages": [],
        "system": "sys",
        "model": cfg.model,
        "settings": settings.seed_defaults(),
        "events": events.EventHooks(),
    }
    state["events"].set_dispatcher(cli._event_dispatcher(state, cfg))
    return state, cfg


def _on(state, cfg, line: str) -> None:
    assert cli._run_command(f"on {line}", state, cfg) == (True, None)


def _user_text(state, text: str) -> str:
    bundle = attach.UserMessageBundle({"role": "user", "content": text}, {"role": "user", "content": text})
    return cli._with_pending_notes(state, bundle).runtime_message["content"]


# --- exec ----------------------------------------------------------------------


def test_new_events_take_handlers():
    hooks = events.EventHooks()
    for name in ("session_start", "session_end", "pre_compact", "post_compact"):
        assert hooks.add(name, "exec true").event == name


def test_handler_stdout_rides_on_the_next_user_message_once(tmp_path):
    state, cfg = _state(tmp_path)
    _on(state, cfg, "session_start exec printf 'on branch main'")

    cli._emit_session_event(state, runtime.Telemetry(None), cfg, "session_start")

    first = _user_text(state, "hello")
    assert "hello" in first
    assert "<js-reminder>on branch main</js-reminder>" in first
    assert "on branch main" not in _user_text(state, "again")


def test_handler_reads_the_event_as_json_on_stdin(tmp_path):
    state, cfg = _state(tmp_path)
    _on(state, cfg, "session_start exec cat")

    cli._emit_session_event(state, runtime.Telemetry(None), cfg, "session_start")

    [note] = state["pending_notes"]
    payload = json.loads(note.removeprefix("<js-reminder>").removesuffix("</js-reminder>"))
    assert payload["event"] == "session_start"
    assert payload["session"] == str(cfg.session_file)


def test_handler_sees_the_event_name_in_its_environment(tmp_path):
    state, cfg = _state(tmp_path)
    _on(state, cfg, "post_compact exec printf \"$JS_EVENT\"")

    state["events"].emit("post_compact", phase="manual")

    assert state["pending_notes"] == ["<js-reminder>post_compact</js-reminder>"]


def test_blank_stdout_queues_nothing(tmp_path):
    state, cfg = _state(tmp_path)
    _on(state, cfg, "session_start exec true")

    cli._emit_session_event(state, runtime.Telemetry(None), cfg, "session_start")

    assert not state.get("pending_notes")


def test_a_failing_handler_queues_nothing_and_records_the_failure(tmp_path):
    state, cfg = _state(tmp_path)
    _on(state, cfg, "session_start exec sh -c 'echo partial; echo broken >&2; exit 3'")

    emission = state["events"].emit("session_start")

    assert not state.get("pending_notes")
    assert emission.results[0].error is not None
    assert emission.results[0].refusal is None


def test_a_handler_past_its_timeout_is_killed(tmp_path):
    state, cfg = _state(tmp_path)
    settings.set_dotted(state["settings"], ("events", "exec_timeout_s"), 1)
    _on(state, cfg, "session_start exec sleep 30")

    emission = state["events"].emit("session_start")

    assert emission.results[0].error is not None
    assert not state.get("pending_notes")


def test_exec_typed_at_the_prompt_queues_its_stdout(tmp_path):
    state, cfg = _state(tmp_path)

    assert cli._run_command("/exec printf 'git status: clean'", state, cfg) == (True, None)

    assert "<js-reminder>git status: clean</js-reminder>" in _user_text(state, "go")


def test_exit_2_outside_tool_call_is_a_failure_not_a_refusal(tmp_path):
    state, cfg = _state(tmp_path)
    _on(state, cfg, "turn_end exec sh -c 'echo nope >&2; exit 2'")

    emission = state["events"].emit("turn_end", reason="stop")

    assert events.refusal_of(emission) is None
    assert emission.results[0].error is not None


# --- tool_call refusal -----------------------------------------------------------


def _run_one_tool_call(tmp_path, state) -> tuple[list[str], list[dict]]:
    ran: list[str] = []

    def echo(text: str):
        ran.append(text)
        return f"echoed {text}"

    registry = ToolRegistry(
        tools=(Tool(name="echo", description="echo", handler=echo,
                    params={"text": {"type": "string"}}, required=("text",)),),
        aliases={"echo": "echo"},
    )
    replies = iter([model_tool_call_result("echo", ['{"text": "hi"}']), model_text_result("done")])
    messages = [{"role": "user", "content": "echo hi"}]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runtime.model_client, "stream_model_async", lambda **_kw: next(replies))
        runtime.run_turn(
            offline_config(tmp_path), "system", messages, runtime.Telemetry(None),
            trace_override=False, tool_registry=registry, tool_context=ToolContext(cwd=tmp_path),
            suppress_output=True, event_hooks=state["events"],
        )
    return ran, [m for m in messages if m.get("role") == "tool"]


def test_tool_call_handler_exit_2_refuses_the_call_with_one_error_line(tmp_path):
    state, cfg = _state(tmp_path)
    _on(state, cfg, "tool_call exec sh -c 'echo \"echo is off limits today\" >&2; exit 2'")

    ran, results = _run_one_tool_call(tmp_path, state)

    assert ran == []
    # The refusal is the result's first line; the retry budget note follows it.
    assert [r["content"].splitlines()[0] for r in results] == ["ERROR: echo is off limits today"]


def test_tool_call_handler_decides_from_the_call_on_stdin(tmp_path):
    state, cfg = _state(tmp_path)
    guard = tmp_path / "guard.sh"
    guard.write_text(
        "payload=$(cat)\n"
        'case "$payload" in *\'"name": "echo"\'*) echo "no echo" >&2; exit 2;; esac\n',
        encoding="utf-8",
    )
    _on(state, cfg, f"tool_call exec sh {guard}")

    ran, results = _run_one_tool_call(tmp_path, state)

    assert ran == []
    assert results[0]["content"].splitlines()[0] == "ERROR: no echo"


def test_tool_call_handler_exit_0_lets_the_call_run(tmp_path):
    state, cfg = _state(tmp_path)
    _on(state, cfg, "tool_call exec true")

    ran, results = _run_one_tool_call(tmp_path, state)

    assert ran == ["hi"]
    assert results[0]["content"] == "echoed hi"


# --- pre_compact / post_compact ------------------------------------------------


def _long_history() -> list[dict]:
    history: list[dict] = []
    for i in range(12):
        history.append({"role": "user", "content": f"question {i} " + "x" * 2000})
        history.append({"role": "assistant", "content": f"answer {i} " + "y" * 2000})
    return history


@pytest.fixture
def summarize_stub(monkeypatch):
    async def summarize(*_args, **_kwargs):
        return "## Summary\nshort"

    monkeypatch.setattr(compaction, "summarize", summarize)


def _compact_cfg(tmp_path):
    return offline_config(tmp_path, settings={"compact": {"tail_tokens": 500, "min_savings_tokens": 1}})


def test_compaction_fires_pre_compact_then_post_compact(tmp_path, summarize_stub):
    heard: list[tuple[str, dict]] = []
    messages = _long_history()

    result = compaction.compact_now_sync(
        _compact_cfg(tmp_path), "sys", messages, forced=True,
        emit=lambda event, **payload: heard.append((event, payload)),
    )

    assert compaction.compacted(result)
    assert [event for event, _ in heard] == ["pre_compact", "post_compact"]
    assert heard[0][1]["messages"] == 24
    assert heard[1][1]["messages"] == len(messages)


def test_a_skipped_compaction_fires_neither(tmp_path, summarize_stub):
    heard: list[str] = []
    messages = [{"role": "user", "content": "hi"}]

    compaction.compact_now_sync(_compact_cfg(tmp_path), "sys", messages,
                                emit=lambda event, **_payload: heard.append(event))

    assert heard == []


def test_slash_compact_runs_the_compact_handlers(tmp_path, summarize_stub):
    state, cfg = _state(tmp_path)
    cfg = _compact_cfg(tmp_path)
    state["events"].set_dispatcher(cli._event_dispatcher(state, cfg))
    state["messages"] = _long_history()
    settings.set_dotted(state["settings"], ("compact", "tail_tokens"), 500)
    settings.set_dotted(state["settings"], ("compact", "min_savings_tokens"), 1)
    _on(state, cfg, "pre_compact exec printf before")
    _on(state, cfg, "post_compact exec printf after")

    assert cli._run_command("/compact", state, cfg) == (True, None)

    assert state["pending_notes"] == ["<js-reminder>before</js-reminder>", "<js-reminder>after</js-reminder>"]


def test_between_turn_auto_compaction_runs_the_compact_handlers(tmp_path, summarize_stub):
    state, cfg = _state(tmp_path)
    cfg = offline_config(tmp_path, settings={"compact": {
        "tail_tokens": 500, "min_savings_tokens": 1, "context_window": 4000, "auto": True}})
    state["settings"] = cfg.settings
    state["events"].set_dispatcher(cli._event_dispatcher(state, cfg))
    state["messages"] = _long_history()
    _on(state, cfg, "post_compact exec printf compacted")

    cli._maybe_auto_compact(cfg, state)

    assert state["pending_notes"] == ["<js-reminder>compacted</js-reminder>"]


# --- session_start / session_end in both REPLs ------------------------------------


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("JS_AGENT", "JS_SESSION", "JS_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_a, **_k: None)
    sent: list[str] = []

    def run_turn_stub(cfg, system, messages, *_a, **_k):
        sent.append(str(messages[-1]["content"]))
        messages.append({"role": "assistant", "content": "ok"})

    async def run_turn_async_stub(cfg, system, messages, *_a, **_k):
        run_turn_stub(cfg, system, messages)

    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.runtime, "run_turn_async", run_turn_async_stub)
    (tmp_path / ".js").mkdir(exist_ok=True)
    log = tmp_path / "ended.log"
    (tmp_path / ".js" / "jsrc").write_text(
        "on session_start exec printf 'you are on branch main'\n"
        f"on session_end exec sh -c 'cat > {log}'\n",
        encoding="utf-8",
    )
    return sent, log


def _stub_prompt_session(monkeypatch, lines):
    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            self.history = history
            self.completer = kwargs.get("completer")
            self.lines = iter(lines)

        def prompt(self, *_args, **_kwargs):
            try:
                return next(self.lines)
            except StopIteration:
                raise EOFError from None

    monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)


def _check_session_events(tmp_path, sent, log):
    reminder = "<js-reminder>you are on branch main</js-reminder>"
    assert sent[0].startswith("first") and reminder in sent[0]
    assert sent[1] == "second"
    [session_file] = list((tmp_path / ".js" / "sessions").rglob(f"{SESSION}.jsonl"))
    assert sum(reminder in str(m.get("content", "")) for m in load_messages(session_file)) == 1
    ended = json.loads(log.read_text(encoding="utf-8"))
    assert ended["event"] == "session_end"
    assert ended["messages"] == 4


def test_blocking_repl_fires_session_start_and_session_end(monkeypatch, tmp_path, home):
    sent, log = home
    _stub_prompt_session(monkeypatch, ["first", "second"])

    cli.main(["--blocking", "--session", SESSION])

    _check_session_events(tmp_path, sent, log)


def test_async_repl_fires_session_start_and_session_end(monkeypatch, tmp_path, home):
    sent, log = home
    _stub_prompt_session(monkeypatch, [])
    lines = ["first", "second"]

    class AppStub:
        def __init__(self, on_line, on_eof):
            self._on_line, self._on_eof = on_line, on_eof

        async def run_async(self):
            for line in lines:
                await self._on_line(line)
            self._on_eof()

        def exit(self):
            pass

        def invalidate(self):
            pass

    monkeypatch.setattr(cli.screen, "build_app",
                        lambda *, on_line, on_eof, **_kw: (AppStub(on_line, on_eof), cli.screen.Scrollback()))
    monkeypatch.setattr(cli.screen, "capture_stdio", lambda *a, **k: contextlib.nullcontext())

    cli.main(["--session", SESSION])

    _check_session_events(tmp_path, sent, log)
