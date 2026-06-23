from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import ai

from js import cli, events, providers, setcmd, settings
from js.config import Config
from js.memory import append_message, load_messages
from js.sampling import Sampling


def _debug_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_cfg(tmp_path: Path) -> Config:
    return Config(
        agent_id="repl-agent",
        agent_dir=tmp_path / ".js" / "sessions" / "repl-agent",
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
        history_file=tmp_path / ".js" / "sessions" / "repl-agent" / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / "repl-agent",
        session_file=tmp_path / ".js" / "sessions" / "repl-agent" / "repl.jsonl",
        prompts_dir=tmp_path / "prompts" / "repl-agent",
    )


def test_reset_command_is_durable_for_later_session_load(tmp_path):
    cfg = make_cfg(tmp_path)
    append_message(cfg.session_file, {"role": "user", "content": "old"})
    state = {"messages": [{"role": "user", "content": "old"}]}

    handled = cli._handle_command("/reset", state, cfg)

    assert handled is True
    assert state["messages"] == []
    assert load_messages(cfg.session_file) == []


def test_refresh_model_catalog_command_forces_refresh(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    seen: list[str] = []

    def refresh_stub() -> bool:
        seen.append("forced")
        print("refreshed")
        return True

    monkeypatch.setattr(cli, "_force_refresh_model_catalog", refresh_stub)

    handled = cli._handle_command("/refresh-model-catalog", {"messages": []}, cfg)

    assert handled is True
    assert seen == ["forced"]
    assert "refreshed" in capsys.readouterr().out


def test_wipe_command_rotates_file_and_clears_in_process_messages(tmp_path):
    cfg = make_cfg(tmp_path)
    append_message(cfg.session_file, {"role": "user", "content": "old"})
    state = {"messages": [{"role": "user", "content": "old"}]}

    handled = cli._handle_command("/wipe", state, cfg)

    assert handled is True
    assert state["messages"] == []
    assert not cfg.session_file.exists()
    assert cfg.session_file.with_suffix(".jsonl.bak").exists()


def test_wipe_preserves_existing_backups_instead_of_overwriting(tmp_path):
    cfg = make_cfg(tmp_path)
    append_message(cfg.session_file, {"role": "user", "content": "first"})
    cli._handle_command("/wipe", {"messages": [{"role": "user", "content": "first"}]}, cfg)
    first_backup = cfg.session_file.with_suffix(".jsonl.bak")
    append_message(cfg.session_file, {"role": "user", "content": "second"})

    handled = cli._handle_command("/wipe", {"messages": [{"role": "user", "content": "second"}]}, cfg)

    assert handled is True
    assert first_backup.exists()
    assert cfg.session_file.with_suffix(".jsonl.bak.1").exists()
    assert load_messages(first_backup) == [{"role": "user", "content": "first"}]
    assert load_messages(cfg.session_file.with_suffix(".jsonl.bak.1")) == [{"role": "user", "content": "second"}]


def test_set_commands_parse_power_user_knobs():
    live_settings = settings.seed_defaults()

    trace = setcmd.run_repl_command(live_settings, "/set runtime.trace on")
    assert trace.error is None
    assert trace.lines == ["runtime.trace = on"]
    assert settings.get_dotted(live_settings, ("runtime", "trace")) is True

    reasoning = setcmd.run_repl_command(live_settings, "/set model.reasoning_effort max")
    assert reasoning.error is None
    assert reasoning.lines == ["model.reasoning_effort = max"]
    assert settings.get_dotted(live_settings, ("model", "reasoning_effort")) == "max"

    maxout = setcmd.run_repl_command(live_settings, "/set model.max_output_tokens 64000")
    assert maxout.error is None
    assert maxout.lines == ["model.max_output_tokens = 64000"]
    assert settings.get_dotted(live_settings, ("model", "max_output_tokens")) == 64000

    cleared = setcmd.run_repl_command(live_settings, "/set model.max_output_tokens off")
    assert cleared.error is None
    assert cleared.lines == ["model.max_output_tokens = <none>"]
    assert settings.get_dotted(live_settings, ("model", "max_output_tokens")) is None


def test_unknown_commands_that_share_prefixes_are_not_swallowed(tmp_path):
    cfg = make_cfg(tmp_path)
    state = {"messages": [], "system": "sys", "settings": settings.seed_defaults()}

    assert cli._handle_command("/setty model.id nope", state, cfg) is False
    assert cli._handle_command("/showtime", state, cfg) is False
    assert cli._handle_command("/compact-autoload", state, cfg) is False


def test_repl_runtime_exception_rolls_back_persisted_user_message(monkeypatch, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    append_message(cfg.session_file, {"role": "user", "content": "existing"})

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["cause failure", "exit"])

        def prompt(self, *args, **kwargs):
            line = next(self.lines)
            if line == "exit":
                return "exit"
            return line

    def run_turn_stub(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    captured = capsys.readouterr()
    assert actual == 0
    assert "RuntimeError: boom" in captured.out
    assert load_messages(cfg.session_file) == [{"role": "user", "content": "existing"}]


def test_repl_keyboard_interrupt_emits_cancel_event(monkeypatch, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    append_message(cfg.session_file, {"role": "user", "content": "existing"})
    seen: list[tuple[str, dict]] = []

    class RecordingHooks(events.EventHooks):
        def emit(self, event: str, **payload):
            seen.append((event, payload))
            return super().emit(event, **payload)

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["interrupt me", "exit"])

        def prompt(self, *args, **kwargs):
            line = next(self.lines)
            if line == "exit":
                return "exit"
            return line

    def run_turn_stub(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.events, "EventHooks", RecordingHooks)
    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert ("input", {"text": "interrupt me", "attachments": []}) in seen
    assert ("cancel", {"reason": "keyboard_interrupt"}) in seen
    assert load_messages(cfg.session_file) == [{"role": "user", "content": "existing"}]


def test_repl_input_hook_error_records_debug_telemetry(monkeypatch, tmp_path):
    debug_log = tmp_path / "debug.log"
    cfg = replace(make_cfg(tmp_path), debug_log=debug_log)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/on input echo nope", "hello", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(*args, **kwargs):
        return None

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    records = _debug_records(debug_log)
    assert actual == 0
    assert {
        "kind": "event_handler_error",
        "event": "input",
        "handler": "echo nope",
        "error": "unsupported event handler command: echo",
    }.items() <= records[0].items()


def test_repl_cancel_hook_error_records_debug_telemetry(monkeypatch, tmp_path):
    debug_log = tmp_path / "debug.log"
    cfg = replace(make_cfg(tmp_path), debug_log=debug_log)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/on cancel echo nope", "interrupt me", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    records = _debug_records(debug_log)
    assert actual == 0
    assert {
        "kind": "event_handler_error",
        "event": "cancel",
        "handler": "echo nope",
        "error": "unsupported event handler command: echo",
    }.items() <= records[0].items()


def test_repl_cancel_hook_partial_load_sampling_change_updates_next_turn(monkeypatch, tmp_path):
    cfg = replace(make_cfg(tmp_path), project_dir=tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    (tmp_path / "cancel.irc").write_text("set sampling.temperature 0.2\nbogus nope\n", encoding="utf-8")
    calls = 0
    temperatures: list[float | None] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/on cancel load cancel.irc", "interrupt me", "hello", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        temperatures.append(kwargs["sampling"].temperature)

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert temperatures == [0.2]


def test_repl_input_hook_dispatches_before_run_turn(monkeypatch, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    trace_overrides: list[bool] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/on input set runtime.trace on", "hello", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(*args, **kwargs):
        trace_overrides.append(kwargs["trace_override"])

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert trace_overrides == [True]


def test_repl_input_hook_does_not_drop_existing_sampling_override(monkeypatch, tmp_path):
    cfg = replace(make_cfg(tmp_path), sampling_cli=Sampling(temperature=0.9))
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    temperatures: list[float | None] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/on input set compact.auto off", "hello", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(*args, **kwargs):
        temperatures.append(kwargs["sampling"].temperature)

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert temperatures == [0.9]


def test_repl_input_hook_sampling_change_updates_turn_sampling(monkeypatch, tmp_path):
    cfg = replace(make_cfg(tmp_path), sampling_cli=Sampling(temperature=0.9))
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    temperatures: list[float | None] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/on input set sampling.temperature 0.2", "hello", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(*args, **kwargs):
        temperatures.append(kwargs["sampling"].temperature)

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert temperatures == [0.2]


def test_repl_input_hook_partial_load_model_change_updates_turn_model(monkeypatch, tmp_path):
    cfg = replace(make_cfg(tmp_path), project_dir=tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    (tmp_path / "model.irc").write_text("set model.id hook-model\nbogus nope\n", encoding="utf-8")
    models: list[str] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/on input load model.irc", "hello", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(cfg_arg, *args, **kwargs):
        models.append(cfg_arg.model)

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert models == ["hook-model"]


def test_repl_input_hook_partial_load_provider_change_updates_turn_config(monkeypatch, tmp_path):
    cfg = replace(make_cfg(tmp_path), project_dir=tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    (tmp_path / "provider.irc").write_text(
        "set provider.id openai\n"
        "set provider.base_url http://provider.test/v1\n"
        "set provider.api_key sk-hook\n"
        "bogus nope\n",
        encoding="utf-8",
    )
    seen: list[tuple[str | None, str | None, str | None]] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/on input load provider.irc", "hello", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(cfg_arg, *args, **kwargs):
        seen.append((cfg_arg.provider_id, cfg_arg.provider_base_url, cfg_arg.provider_api_key))

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert seen == [("openai", "http://provider.test/v1", "sk-hook")]


def test_repl_input_hook_partial_load_tool_aliases_update_turn_config(monkeypatch, tmp_path):
    cfg = replace(make_cfg(tmp_path), project_dir=tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    (tmp_path / "tools.irc").write_text(
        'set tools.alias_profiles [{"match":["offline-test-model"],"aliases":{"read":"r"}}]\n'
        "bogus nope\n",
        encoding="utf-8",
    )
    seen: list[list[dict]] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/on input load tools.irc", "hello", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(cfg_arg, *args, **kwargs):
        seen.append(cfg_arg.settings.get("tools", {}).get("alias_profiles", []))

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert seen == [[{"match": ["offline-test-model"], "aliases": {"read": "r"}}]]


def test_repl_set_limit_updates_turn_config(monkeypatch, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    max_tool_result_bytes: list[int] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/set limits.max_tool_result_bytes 123", "hello", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(cfg_arg, *args, **kwargs):
        max_tool_result_bytes.append(cfg_arg.max_tool_result_bytes)

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert max_tool_result_bytes == [123]


def test_repl_set_artifact_settings_update_turn_config(monkeypatch, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    artifacts: list[tuple[str | None, str | None, str | None]] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter([
                "/set artifact.dir /tmp/live-artifacts",
                "/set artifact.url http://artifact.live/",
                "/set artifact.bin artifact-live",
                "hello",
                "exit",
            ])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(cfg_arg, *args, **kwargs):
        artifacts.append((cfg_arg.artifact_dir, cfg_arg.artifact_url, cfg_arg.artifact_bin))

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert artifacts == [("/tmp/live-artifacts", "http://artifact.live/", "artifact-live")]


def test_repl_set_subagent_prefer_inherit_updates_turn_config(monkeypatch, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    prefer_inherit: list[bool] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/set subagents.prefer_inherit on", "hello", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(cfg_arg, *args, **kwargs):
        prefer_inherit.append(cfg_arg.prefer_inherit)

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert prefer_inherit == [True]


def test_repl_set_max_output_updates_turn_config(monkeypatch, tmp_path):
    cfg = replace(make_cfg(tmp_path), max_output_tokens=99)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    max_output_tokens: list[int | None] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/set model.max_output_tokens off", "hello", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(cfg_arg, *args, **kwargs):
        max_output_tokens.append(cfg_arg.max_output_tokens)

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert max_output_tokens == [None]


def test_repl_turn_end_hook_partial_load_sampling_change_updates_next_turn(monkeypatch, tmp_path):
    cfg = replace(make_cfg(tmp_path), project_dir=tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    (tmp_path / "turn-end.irc").write_text("set sampling.temperature 0.2\nbogus nope\n", encoding="utf-8")
    calls = 0
    temperatures: list[float | None] = []

    class SessionStub:
        def __init__(self, history=None, **kwargs):
            self.lines = iter(["/on turn_end load turn-end.irc", "first", "second", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            kwargs["event_hooks"].emit("turn_end", reason="stop")
            return None
        temperatures.append(kwargs["sampling"].temperature)
        return None

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    assert temperatures == [0.2]


def test_provider_commands_mutate_runtime_state(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
    }

    assert cli._handle_command("/provider openai", state, cfg) is True
    assert cli._handle_command("/baseurl http://localhost:11434/v1", state, cfg) is True
    assert cli._handle_command("/apikey ollama", state, cfg) is True

    assert state["provider_id"] == "openai"
    assert state["provider_base_url"] == "http://localhost:11434/v1"
    assert state["provider_api_key"] == "ollama"


def test_first_class_provider_shortcuts_use_defaults(tmp_path, capsys, monkeypatch):
    # /provider <name> must resolve provider DEFAULTS — isolate from the operator's
    # real saved logins + provider env overrides, or a saved ollama login (e.g. a
    # Tailscale URL) leaks in and the test snaps on the real box.
    for var in (
        "OLLAMA_BASE_URL", "OLLAMA_LOCAL_BASE_URL", "OLLAMA_API_KEY", "OLLAMA_LOCAL_API_KEY",
        "LLAMACPP_BASE_URL", "LLAMA_CPP_BASE_URL", "LLAMACPP_API_KEY", "LLAMA_CPP_API_KEY",
        "XIAOMI_TP_BASE_URL", "MIMO_TP_BASE_URL", "XIAOMI_TP_KEY", "MIMO_TP_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli.logins, "load_logins", lambda *a, **k: {})

    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
    }

    assert cli._handle_command("/provider ollama", state, cfg) is True
    assert state["provider_id"] == "ollama"
    assert state["provider_base_url"] == providers.provider_base_url(providers.provider_for_login("ollama"), None)
    assert state["provider_api_key"] == providers.provider_api_key(providers.provider_for_login("ollama"), None)

    assert cli._handle_command("/provider llama.cpp", state, cfg) is True
    assert state["provider_id"] == "llama.cpp"
    assert state["provider_base_url"] == providers.provider_base_url(providers.provider_for_login("llama.cpp"), None)
    assert state["provider_api_key"] == providers.provider_api_key(providers.provider_for_login("llama.cpp"), None)

    assert cli._handle_command("/provider mimo-token-plan", state, cfg) is True
    assert state["provider_id"] == "mimo-token-plan"
    assert state["provider_base_url"] == providers.provider_base_url(providers.provider_for_login("mimo-token-plan"), None)
    assert state["provider_api_key"] == providers.provider_api_key(providers.provider_for_login("mimo-token-plan"), None)
    out = capsys.readouterr().out
    assert "ollama" in out
    assert "llama.cpp" in out
    assert "mimo-token-plan" in out


def test_login_loads_saved_provider_bundle(tmp_path):
    from js import logins

    cfg = make_cfg(tmp_path)
    logins.set_config_dir(tmp_path / "login-store")
    logins.save_login(
        logins.Login(
            provider_id="openai",
            sdk_provider_id="openai",
            provider_base_url="http://localhost:11434/v1",
            provider_api_key="ollama",
        )
    )
    state = {
        "model": cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
    }

    try:
        assert cli._handle_command("/login openai", state, cfg) is True
        assert state["provider_id"] == "openai"
        assert state["provider_base_url"] == "http://localhost:11434/v1"
        assert state["provider_api_key"] == "ollama"
    finally:
        logins.set_config_dir(None)


def test_logout_clears_provider_state(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": "openai",
        "provider_base_url": "http://localhost:11434/v1",
        "provider_api_key": "ollama",
    }

    assert cli._handle_command("/logout", state, cfg) is True

    assert state["provider_id"] is None
    assert state["provider_base_url"] is None
    assert state["provider_api_key"] is None


def test_models_command_lists_provider_models(monkeypatch, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": "openai",
        "provider_base_url": "http://localhost:11434/v1",
        "provider_api_key": "ollama",
    }

    async def fake_list_models():
        return ["model-a", "model-b", "model-c"]

    class FakeProvider:
        async def list_models(self):
            return await fake_list_models()

    monkeypatch.setattr(ai, "get_provider", lambda *args, **kwargs: FakeProvider())

    assert cli._handle_command("/models 2", state, cfg) is True

    out = capsys.readouterr().out
    assert "model-a" in out
    assert "model-b" in out
    assert "model-c" not in out
    assert "1 more" in out


def test_models_command_refuses_without_provider(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }

    assert cli._handle_command("/models", state, cfg) is True

    assert "no provider set" in capsys.readouterr().out


def test_provider_command_shows_current_value_when_bare(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": "openai",
        "provider_base_url": None,
        "provider_api_key": None,
    }

    assert cli._handle_command("/provider", state, cfg) is True
    assert "openai" in capsys.readouterr().out



def test_model_command_opens_picker_and_updates_state(monkeypatch, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "messages": [],
        "system": "SYSTEM",
        "model": cfg.model,
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }

    monkeypatch.setattr(
        cli.picker,
        "pick_model",
        lambda **kwargs: {
            "provider_id": "deepseek",
            "provider_base_url": None,
            "provider_api_key": "sk-test",
            "provider_headers": {},
            "model": "deepseek-v4-flash",
        },
    )

    assert cli._handle_command("/model", state, cfg) is True
    assert state["provider_id"] == "deepseek"
    assert state["provider_api_key"] == "sk-test"
    assert state["model"] == "deepseek-v4-flash"



def test_model_command_with_prefixed_provider_resets_provider_state(tmp_path):
    from js import logins

    cfg = replace(
        make_cfg(tmp_path),
        provider_id="deepseek",
        provider_base_url="https://api.deepseek.com",
        provider_api_key="sk-deepseek",
    )
    logins.set_config_dir(tmp_path / "login-store")
    logins.save_login(
        logins.Login(
            provider_id="ollama",
            provider_base_url="http://ollama.test/v1",
            provider_api_key="ollama",
        )
    )
    state = {
        "messages": [],
        "system": "SYSTEM",
        "model": cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
        "provider_headers": {"x-deepseek": "1"},
    }
    try:
        assert cli._handle_command("/model ollama/gemma4:e2b", state, cfg) is True
        assert state["provider_id"] == "ollama"
        assert state["provider_base_url"] == "http://ollama.test/v1"
        assert state["provider_api_key"] == "ollama"
        assert state["provider_headers"] == {}
        assert state["model"] == "gemma4:e2b"
    finally:
        logins.set_config_dir(None)

def test_pick_model_command_opens_picker_and_updates_state(monkeypatch, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "messages": [],
        "system": "SYSTEM",
        "model": cfg.model,
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }

    monkeypatch.setattr(
        cli.picker,
        "pick_model",
        lambda **kwargs: {
            "provider_id": "openai",
            "provider_base_url": "http://proxy.test/v1",
            "provider_api_key": "sk-proxy",
            "provider_headers": {"x-proxy": "1"},
            "model": "proxy/model",
        },
    )

    assert cli._handle_command("/pick-model", state, cfg) is True
    assert state["provider_id"] == "openai"
    assert state["provider_base_url"] == "http://proxy.test/v1"
    assert state["provider_api_key"] == "sk-proxy"
    assert state["provider_headers"] == {"x-proxy": "1"}
    assert state["model"] == "proxy/model"
    assert "openai" in capsys.readouterr().out

def test_provider_command_uses_saved_login(tmp_path):
    from js import logins

    cfg = make_cfg(tmp_path)
    logins.set_config_dir(tmp_path / "login-store")
    logins.save_login(
        logins.Login(
            provider_id="my-proxy",
            sdk_provider_id="openai",
            provider_base_url="http://proxy.test/v1",
            provider_api_key="sk-proxy",
        )
    )
    state = {
        "model": cfg.model,
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }
    try:
        assert cli._handle_command("/provider my-proxy", state, cfg) is True
        assert state["provider_id"] == "my-proxy"
        assert state["provider_base_url"] == "http://proxy.test/v1"
        assert state["provider_api_key"] == "sk-proxy"
    finally:
        logins.set_config_dir(None)



def test_picker_open_preserves_explicit_logout_over_config(monkeypatch, tmp_path):
    cfg = replace(
        make_cfg(tmp_path),
        provider_id="openai",
        provider_base_url="http://cfg.test/v1",
        provider_api_key="cfg-key",
    )
    state = {
        "messages": [],
        "system": "SYSTEM",
        "model": cfg.model,
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }
    captured = {}

    def fake_pick_model(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(cli.picker, "pick_model", fake_pick_model)

    assert cli._handle_command("/model", state, cfg) is True
    assert captured["provider_id"] is None
    assert captured["provider_base_url"] is None
    assert captured["provider_api_key"] is None

def test_cfg_for_active_model_carries_provider_overrides(tmp_path):
    cfg = make_cfg(tmp_path)
    state = {
        "model": "other-model",
        "provider_id": None,
        "provider_base_url": "http://proxy.test/v1",
        "provider_api_key": "sk-proxy",
    }

    active = cli._cfg_for_active_model(cfg, state)
    assert active.model == "other-model"
    assert active.provider_id is None
    assert active.provider_base_url == "http://proxy.test/v1"
    assert active.provider_api_key == "sk-proxy"


def test_cfg_for_active_model_keeps_pinned_provider_for_prefixed_model(tmp_path):
    # A gateway/openrouter model id like "anthropic/claude-..." selected under an
    # explicitly pinned provider must NOT let the prefix hijack the provider or
    # inherit the gateway's base/api/headers onto a native provider transport.
    cfg = make_cfg(tmp_path)
    state = {
        "model": "anthropic/claude-sonnet-4",
        "provider_id": "omp",
        "provider_base_url": "https://gateway.test/v1",
        "provider_api_key": "sk-omp",
        "provider_headers": {"x-omp": "1"},
    }

    active = cli._cfg_for_active_model(cfg, state)
    assert active.provider_id == "omp"
    assert active.model == "anthropic/claude-sonnet-4"
    assert active.provider_base_url == "https://gateway.test/v1"
    assert active.provider_api_key == "sk-omp"
    assert active.provider_headers == {"x-omp": "1"}


def test_cfg_for_active_model_strips_same_provider_prefix(tmp_path):
    # When the prefix names the pinned provider, the prefix is stripped without
    # disturbing the pinned credentials.
    cfg = make_cfg(tmp_path)
    state = {
        "model": "deepseek/deepseek-v4-flash",
        "provider_id": "deepseek",
        "provider_base_url": "https://api.deepseek.com",
        "provider_api_key": "sk-deepseek",
    }

    active = cli._cfg_for_active_model(cfg, state)
    assert active.provider_id == "deepseek"
    assert active.model == "deepseek-v4-flash"
    assert active.provider_base_url == "https://api.deepseek.com"
    assert active.provider_api_key == "sk-deepseek"


def test_cfg_for_active_model_routes_prefix_when_provider_unset(tmp_path):
    # With no explicit provider, the model prefix still routes (AI-gateway case).
    cfg = make_cfg(tmp_path)
    state = {
        "model": "deepseek/deepseek-v4-flash",
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }

    active = cli._cfg_for_active_model(cfg, state)
    assert active.provider_id == "deepseek"
    assert active.model == "deepseek-v4-flash"
