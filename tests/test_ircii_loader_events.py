from __future__ import annotations

from pathlib import Path

from repl_driver import record_turns, run_async

from js import events, repl_commands, setcmd, settings
from js.config import Config
from js.repl_state import ReplState


def make_cfg(tmp_path: Path) -> Config:
    d = tmp_path / ".js" / "sessions" / "a"
    return Config(
        agent_id="a",
        agent_dir=d,
        model="offline-test-model",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=d / ".history",
        sessions_dir=d,
        session_file=d / "s.jsonl",
        prompts_dir=tmp_path / "prompts" / "a",
        project_dir=tmp_path,
    )


def make_state() -> ReplState:
    return ReplState(messages=[], system="sys", settings=settings.seed_defaults(), events=events.EventHooks())


def _type(monkeypatch, tmp_path, lines) -> tuple[ReplState, list]:
    """Type `lines` and then `hello` into the REPL on make_cfg, in `tmp_path`
    on the stock settings: the state it leaves and the one turn `hello` ran."""
    monkeypatch.chdir(tmp_path)
    cfg = make_cfg(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "01-prompt.md").write_text("sys\n", encoding="utf-8")
    turns = record_turns(monkeypatch)
    state = run_async(monkeypatch, cfg, [*lines, "hello"], wait=True, live_settings=settings.seed_defaults())
    [turn] = turns
    assert turn.messages[-1]["content"] == "hello"
    return state, turn


def test_load_applies_slashless_script_lines(tmp_path, monkeypatch):
    script = tmp_path / "boot.irc"
    script.write_text(
        "set TRANSCRIPT_LOG off\n"
        "set EXTRA_PARAMS.organization ./wiki\n",
        encoding="utf-8",
    )

    _state, turn = _type(monkeypatch, tmp_path, ["/load boot.irc"])

    assert settings.get_dotted(turn.settings, ("TRANSCRIPT_LOG",)) is False
    assert settings.get_dotted(turn.settings, ("EXTRA_PARAMS", "organization")) == "./wiki"


def test_script_load_treats_bare_slash_line_as_noop(tmp_path):
    script = tmp_path / "bare-slash.irc"
    script.write_text("/\nset COMPACTION_AUTO off\n", encoding="utf-8")
    state = make_state()

    assert repl_commands.run_command("/load bare-slash.irc", state, make_cfg(tmp_path)) == (True, None)
    assert settings.get_dotted(state.settings, ("COMPACTION_AUTO",)) is False


def test_script_load_resolves_nested_loads_relative_to_current_script(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "inner.irc").write_text("set OUTPUT_TOKENS_MAX 123\n", encoding="utf-8")
    outer = scripts / "outer.irc"
    outer.write_text("load inner.irc\n", encoding="utf-8")

    _state, turn = _type(monkeypatch, tmp_path, [f"/load {outer}"])

    assert settings.get_dotted(turn.settings, ("OUTPUT_TOKENS_MAX",)) == 123


def test_script_load_allows_comment_after_nested_load_path(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "inner.irc").write_text("set COMPACTION_AUTO off\n", encoding="utf-8")
    outer = scripts / "outer.irc"
    outer.write_text("load inner.irc # shared event/settings bootstrap\n", encoding="utf-8")

    _state, turn = _type(monkeypatch, tmp_path, [f"/load {outer}"])

    assert settings.get_dotted(turn.settings, ("COMPACTION_AUTO",)) is False


def test_script_load_stops_at_the_first_error_and_names_every_file_on_the_way(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    inner = scripts / "inner.irc"
    inner.write_text("set COMPACTION_AUTO off\nbogus nope\nset OUTPUT_TOKENS_MAX 99\n", encoding="utf-8")
    outer = scripts / "outer.irc"
    outer.write_text("load inner.irc\n", encoding="utf-8")
    state = make_state()

    _, error = repl_commands.run_command(f"/load {outer}", state, make_cfg(tmp_path))

    assert f"{outer}:1" in error
    assert f"{inner}:2" in error
    assert settings.get_dotted(state.settings, ("COMPACTION_AUTO",)) is False
    assert settings.get_dotted(state.settings, ("OUTPUT_TOKENS_MAX",)) is None


def test_script_load_refuses_a_cycle(tmp_path):
    loop = tmp_path / "loop.irc"
    loop.write_text("set COMPACTION_AUTO off\nload loop.irc\n", encoding="utf-8")
    state = make_state()

    handled, error = repl_commands.run_command("/load loop.irc", state, make_cfg(tmp_path))

    assert handled is True
    assert f"{loop}:2" in error
    assert settings.get_dotted(state.settings, ("COMPACTION_AUTO",)) is False
    assert state.load_stack == []


def test_script_load_reports_read_errors_without_raising(tmp_path):
    script = tmp_path / "bad.irc"
    script.write_bytes(b"\xff")
    state = make_state()
    before = settings.seed_defaults()

    handled, error = repl_commands.run_command("/load bad.irc", state, make_cfg(tmp_path))

    assert handled is True
    assert str(script) in error
    assert state.settings == before


def test_load_runs_every_command_in_the_table(tmp_path, monkeypatch):
    """A file of commands is not limited to settings verbs: `model`, `provider`,
    `alias` and `on` lines all dispatch through the command table."""
    monkeypatch.setenv("HOME", str(tmp_path))
    script = tmp_path / "all.irc"
    script.write_text(
        "provider deepseek\n"
        "model deepseek-chat\n"
        "alias ca compact-auto $*\n"
        "ca off\n"
        "on turn_start set TRANSCRIPT_LOG on\n",
        encoding="utf-8",
    )

    state, turn = _type(monkeypatch, tmp_path, ["/load all.irc"])

    assert turn.cfg.provider_id == "deepseek"
    assert turn.cfg.model == "deepseek-chat"
    assert state.aliases == {"ca": "compact-auto $*"}
    assert settings.get_dotted(turn.settings, ("COMPACTION_AUTO",)) is False
    assert [hook.handler for hook in state.events.handlers_for("turn_start")] == ["set TRANSCRIPT_LOG on"]


def test_on_registers_handlers_against_typed_event_names():
    hooks = events.EventHooks()

    result = setcmd.on_command(hooks, "^tool_call echo denied")

    assert result.error is None
    assert hooks.handlers_for("tool_call") == [events.EventHook(event="tool_call", handler="echo denied", suppress=True)]


def test_on_rejects_unknown_event_without_registering():
    hooks = events.EventHooks()

    result = setcmd.on_command(hooks, "nope echo no")

    assert result.changed is False
    assert result.error == "unknown event: nope"
    assert hooks.all() == events.EventHooks().all()


def _repl_made_state(monkeypatch, tmp_path) -> ReplState:
    """The state the REPL on make_cfg leaves, on the stock settings."""
    state, _turn = _type(monkeypatch, tmp_path, [])
    return state


def test_event_hook_dispatches_handler_through_the_command_table(tmp_path, monkeypatch):
    state = _repl_made_state(monkeypatch, tmp_path)
    hooks = state.events
    hooks.add("turn_start", "set COMPACTION_AUTO off")
    hooks.add("turn_start", "alias hi turns")

    emission = hooks.emit("turn_start", model="offline-test-model")

    assert settings.get_dotted(state.settings, ("COMPACTION_AUTO",)) is False
    assert state.aliases == {"hi": "turns"}
    assert [result.error for result in emission.results] == [None, None]


def test_event_hook_handler_errors_are_captured_without_raising(tmp_path, monkeypatch):
    state = _repl_made_state(monkeypatch, tmp_path)
    hooks = state.events
    hooks.add("turn_start", "echo nope")
    hooks.add("turn_start", "set nope.nope.nope x")

    emission = hooks.emit("turn_start")

    assert all(result.error for result in emission.results)
    assert settings.get_dotted(state.settings, ("COMPACTION_AUTO",)) is True


def test_event_hooks_skip_recursive_dispatch():
    hooks = events.EventHooks()
    calls: list[str] = []

    def recursive_dispatch(hook: events.EventHook, emission: events.EventEmission):
        calls.append(f"{emission.event}:{hook.handler}")
        nested = hooks.emit("turn_start", nested=True)
        assert nested.dispatch_skipped is True
        assert nested.results == []
        return events.EventHandlerResult(hook=hook, error="ok")

    hooks.set_dispatcher(recursive_dispatch)
    hooks.add("turn_start", "set COMPACTION_AUTO off")

    emission = hooks.emit("turn_start")

    assert calls == ["turn_start:set COMPACTION_AUTO off"]
    assert emission.dispatch_skipped is False
    assert emission.results[0].error == "ok"


def test_cli_load_updates_live_settings_and_event_hooks(tmp_path, monkeypatch):
    script = tmp_path / "agent.irc"
    script.write_text(
        "set COMPACTION_AUTO off\n"
        "on turn_start echo boot\n",
        encoding="utf-8",
    )

    state, turn = _type(monkeypatch, tmp_path, [f"/load {script.name}"])

    assert settings.get_dotted(turn.settings, ("COMPACTION_AUTO",)) is False
    assert state.events.handlers_for("turn_start") == [
        events.EventHook(event="turn_start", handler="echo boot", suppress=False)
    ]


def test_cli_load_partial_event_hook_output_keeps_script_order(tmp_path):
    script = tmp_path / "agent-error.irc"
    script.write_text("on turn_start echo boot\nbogus nope\n", encoding="utf-8")
    cfg = make_cfg(tmp_path)
    hooks = events.EventHooks()
    state = ReplState(
        messages=[],
        system="sys",
        settings=settings.seed_defaults(),
        events=hooks,
    )

    handled, error = repl_commands.run_command(f"/load {script.name}", state, cfg)

    assert handled is True
    assert f"{script}:2" in error
    assert hooks.handlers_for("turn_start") == [
        events.EventHook(event="turn_start", handler="echo boot", suppress=False)
    ]


def test_cli_load_sampling_set_updates_live_sampling_override(tmp_path, monkeypatch):
    script = tmp_path / "sampling.irc"
    script.write_text("set TEMPERATURE 0.2\n", encoding="utf-8")

    _state, turn = _type(monkeypatch, tmp_path, [f"/load {script.name}"])

    assert turn.cfg.layer_set["TEMPERATURE"] == 0.2
    assert turn.kwargs["sampling"].temperature == 0.2
