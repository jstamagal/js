from __future__ import annotations

from pathlib import Path

from js import cli, events, setcmd, settings
from js.config import Config


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


def test_repl_load_applies_slashless_script_lines(tmp_path):
    script = tmp_path / "boot.irc"
    script.write_text(
        "set runtime.trace off\n"
        "set wiki.aliases.creative ./wiki\n",
        encoding="utf-8",
    )
    live_settings = settings.seed_defaults()
    ctx = setcmd.CommandContext(cwd=tmp_path, events=events.EventHooks())

    result = setcmd.run_repl_command(live_settings, "/load boot.irc", context=ctx)

    assert result.handled is True
    assert result.changed is True
    assert result.error is None
    assert settings.get_dotted(live_settings, ("runtime", "trace")) is False
    assert settings.get_dotted(live_settings, ("wiki", "aliases", "creative")) == "./wiki"
    assert result.lines[-1] == f"loaded {script}"


def test_script_load_resolves_nested_loads_relative_to_current_script(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "inner.irc").write_text("set model.max_output_tokens 123\n", encoding="utf-8")
    outer = scripts / "outer.irc"
    outer.write_text("load inner.irc\n", encoding="utf-8")
    live_settings = settings.seed_defaults()
    ctx = setcmd.CommandContext(cwd=tmp_path, events=events.EventHooks())

    result = setcmd.run_repl_command(live_settings, f"/load {outer}", context=ctx)

    assert result.error is None
    assert settings.get_dotted(live_settings, ("model", "max_output_tokens")) == 123


def test_on_registers_handlers_against_typed_event_names():
    hooks = events.EventHooks()
    live_settings = settings.seed_defaults()
    ctx = setcmd.CommandContext(events=hooks)

    result = setcmd.apply_script_line(live_settings, "on ^tool_call echo denied", context=ctx)

    assert result.handled is True
    assert result.changed is True
    assert result.error is None
    registered = hooks.handlers_for("tool_call")
    assert registered == [events.EventHook(event="tool_call", handler="echo denied", suppress=True)]
    assert result.lines == ["on ^tool_call = echo denied"]


def test_on_rejects_unknown_event_without_registering():
    hooks = events.EventHooks()
    ctx = setcmd.CommandContext(events=hooks)

    result = setcmd.apply_script_line(settings.seed_defaults(), "on nope echo no", context=ctx)

    assert result.handled is True
    assert result.changed is False
    assert result.error == "unknown event: nope"
    assert hooks.handlers_for("nope") == []


def test_event_hook_dispatches_setcmd_handler_against_live_settings(tmp_path):
    hooks = events.EventHooks()
    live_settings = settings.seed_defaults()
    hooks.set_dispatcher(
        setcmd.EventCommandDispatcher(settings=live_settings, cwd=tmp_path, events=hooks)
    )
    hooks.add("turn_start", "set compact.auto off")

    emission = hooks.emit("turn_start", model="offline-test-model")

    assert settings.get_dotted(live_settings, ("compact", "auto")) is False
    assert emission.dispatch_skipped is False
    assert len(emission.results) == 1
    assert emission.results[0].changed is True
    assert emission.results[0].error is None
    assert emission.results[0].lines == ["compact.auto = off"]


def test_event_hook_handler_errors_are_captured_without_raising(tmp_path):
    hooks = events.EventHooks()
    live_settings = settings.seed_defaults()
    hooks.set_dispatcher(
        setcmd.EventCommandDispatcher(settings=live_settings, cwd=tmp_path, events=hooks)
    )
    hooks.add("turn_start", "echo nope")

    emission = hooks.emit("turn_start")

    assert emission.results[0].error == "unsupported event handler command: echo"
    assert settings.get_dotted(live_settings, ("compact", "auto")) is True


def test_event_hooks_skip_recursive_dispatch():
    hooks = events.EventHooks()
    calls: list[str] = []

    def recursive_dispatch(hook: events.EventHook, emission: events.EventEmission):
        calls.append(f"{emission.event}:{hook.handler}")
        nested = hooks.emit("turn_start", nested=True)
        assert nested.dispatch_skipped is True
        assert nested.results == []
        return events.EventHandlerResult(hook=hook, lines=["ok"])

    hooks.set_dispatcher(recursive_dispatch)
    hooks.add("turn_start", "set compact.auto off")

    emission = hooks.emit("turn_start")

    assert calls == ["turn_start:set compact.auto off"]
    assert emission.dispatch_skipped is False
    assert emission.results[0].lines == ["ok"]


def test_cli_load_updates_live_settings_and_event_hooks(tmp_path):
    script = tmp_path / "agent.irc"
    script.write_text(
        "set compact.auto off\n"
        "on turn_start echo boot\n",
        encoding="utf-8",
    )
    cfg = make_cfg(tmp_path)
    hooks = events.EventHooks()
    state = {
        "messages": [],
        "system": "sys",
        "settings": settings.seed_defaults(),
        "events": hooks,
    }

    assert cli._handle_command(f"/load {script.name}", state, cfg) is True
    assert settings.get_dotted(state["settings"], ("compact", "auto")) is False
    assert hooks.handlers_for("turn_start") == [
        events.EventHook(event="turn_start", handler="echo boot", suppress=False)
    ]


def test_cli_load_sampling_set_updates_live_sampling_override(tmp_path):
    script = tmp_path / "sampling.irc"
    script.write_text("set sampling.temperature 0.2\n", encoding="utf-8")
    cfg = make_cfg(tmp_path)
    state = {
        "messages": [],
        "system": "sys",
        "settings": settings.seed_defaults(),
        "events": events.EventHooks(),
        "sampling_cli": cfg.sampling_cli,
    }

    assert cli._handle_command(f"/load {script.name}", state, cfg) is True
    assert state["sampling_cli"].temperature == 0.2
