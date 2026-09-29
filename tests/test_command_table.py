"""The one command table: /save writes everything resident, a restart replays
it, and a jsrc may hold any command line."""

from __future__ import annotations

import contextlib

from js import cli, paths, settings


def _drive_repl(monkeypatch, tmp_path, lines, run_turn_async_stub=None):
    """Run `js` (async REPL) headless: each line hits the Enter handler, then EOF."""
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    class AppStub:
        def __init__(self, on_line, on_eof):
            self._on_line, self._on_eof = on_line, on_eof

        async def run_async(self):
            for line in lines:
                await self._on_line(line.strip())
            self._on_eof()

        def exit(self):
            pass

        def invalidate(self):
            pass

    def build_app_stub(*, on_line, on_eof, **_kwargs):
        return AppStub(on_line, on_eof), cli.screen.Scrollback()

    async def no_turn(*args, **kwargs):
        raise AssertionError("no turn expected")

    monkeypatch.setattr(cli.screen, "build_app", build_app_stub)
    monkeypatch.setattr(cli.screen, "capture_stdio", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(cli.runtime, "run_turn_async", run_turn_async_stub or no_turn)
    return cli.main([])


def _capture_startup_state(monkeypatch) -> dict:
    captured: dict = {}
    real = cli._run_rc_commands

    def spy(state, cfg, rc_paths):
        errors = real(state, cfg, rc_paths)
        captured["state"], captured["errors"] = state, errors
        return errors

    monkeypatch.setattr(cli, "_run_rc_commands", spy)
    return captured


def test_save_then_restart_brings_back_handlers_and_aliases(monkeypatch, tmp_path):
    assert _drive_repl(monkeypatch, tmp_path, [
        "/on turn_start set compact.auto off",
        "/on ^tool_call set runtime.trace on",
        "/alias ca compact-auto $*",
        "/save",
    ]) == 0

    captured = _capture_startup_state(monkeypatch)
    assert _drive_repl(monkeypatch, tmp_path, []) == 0

    state = captured["state"]
    assert captured["errors"] == []
    assert [h.handler for h in state["events"].handlers_for("turn_start")] == ["set compact.auto off"]
    assert [(h.handler, h.suppress) for h in state["events"].handlers_for("tool_call")] == [
        ("set runtime.trace on", True)
    ]
    assert state["aliases"] == {"ca": "compact-auto $*"}


def test_save_writes_only_non_default_settings(monkeypatch, tmp_path):
    _drive_repl(monkeypatch, tmp_path, ["/set compact.auto off", "/save"])

    saved = paths.global_config_file().read_text(encoding="utf-8")
    reloaded = settings.collect_settings(config_paths=[paths.global_config_file()], env={})

    assert settings.get_dotted(reloaded, ("compact", "auto")) is False
    assert "limits.max_tool_iterations" not in saved


def test_jsrc_model_and_provider_lines_take_effect(monkeypatch, tmp_path):
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("/provider deepseek\n/model deepseek-chat\n", encoding="utf-8")
    seen = {}

    async def run_turn_async_stub(cfg, system, messages, telemetry, **kwargs):
        seen["model"], seen["provider"] = cfg.model, cfg.provider_id
        messages.append({"role": "assistant", "content": "ok"})

    assert _drive_repl(monkeypatch, tmp_path, ["hello"], run_turn_async_stub) == 0

    assert seen == {"model": "deepseek-chat", "provider": "deepseek"}


def test_rc_replay_leaves_set_lines_to_the_settings_layer(monkeypatch, tmp_path):
    """Env beats jsrc for settings; replaying the rc at startup must not undo that."""
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("set compact.auto off\nalias t turns\n", encoding="utf-8")
    monkeypatch.setenv("JS_COMPACT_AUTO", "on")
    captured = _capture_startup_state(monkeypatch)

    _drive_repl(monkeypatch, tmp_path, [])

    assert settings.get_dotted(captured["state"]["settings"], ("compact", "auto")) is True
    assert captured["state"]["aliases"] == {"t": "turns"}


def test_rc_errors_name_the_line_and_do_not_stop_startup(monkeypatch, tmp_path):
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("bogus verb\nalias t turns\n", encoding="utf-8")
    captured = _capture_startup_state(monkeypatch)

    assert _drive_repl(monkeypatch, tmp_path, []) == 0

    assert len(captured["errors"]) == 1
    assert f"{jsrc}:1" in captured["errors"][0]
    assert captured["state"]["aliases"] == {"t": "turns"}


def _state() -> dict:
    return {"messages": [], "system": "sys", "settings": settings.seed_defaults()}


def test_alias_runs_its_body_with_arguments(tmp_path):
    state = _state()
    cli._handle_command("/alias ca compact-auto $*", state, None)
    cli._handle_command("/alias trace set runtime.trace", state, None)

    assert cli._handle_command("/ca off", state, None) is True
    assert cli._handle_command("/trace off", state, None) is True

    assert settings.get_dotted(state["settings"], ("compact", "auto")) is False
    assert settings.get_dotted(state["settings"], ("runtime", "trace")) is False


def test_alias_cannot_shadow_a_command_and_can_be_removed():
    state = _state()
    cli._handle_command("/alias set turns", state, None)
    cli._handle_command("/alias t turns", state, None)
    cli._handle_command("/alias -t", state, None)

    assert state["aliases"] == {}


def test_self_referencing_alias_stops():
    state = _state()
    cli._handle_command("/alias loop loop", state, None)

    handled, error = cli._run_command("loop", state, None)

    assert handled is True
    assert error


def test_unknown_slash_word_is_not_a_command():
    """A line like `/usr/bin/python is broken` is prose for the model."""
    assert cli._handle_command("/usr/bin/python is broken", _state(), None) is False


def test_turn_state_commands_come_from_the_table(monkeypatch):
    command = cli.Command(lambda arg, state, cfg: None, "scrub", "test entry", turn_state=True)
    monkeypatch.setitem(cli.COMMANDS, "scrub", command)

    assert cli._is_turn_state_command("/scrub now")
    assert not cli._is_turn_state_command("/turns")
