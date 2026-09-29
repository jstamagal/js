from __future__ import annotations

from unittest.mock import AsyncMock

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
import pytest

from js import cli, runtime, settings
from js import messages as msgs
from js.config import Config
from js.memory import load_messages
from js.model_client import ModelStreamResult
from repl_driver import repl_state

@pytest.fixture(autouse=True)
def fresh_tool_context(monkeypatch, tmp_path):
    from js.toolkit.core import ToolContext

    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))


def _fake_stream_result(text: str = "ok"):
    """Return a ModelStreamResult with text, no tool calls, no reasoning."""
    import ai.types.usage
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )

def _prompt_cfg(tmp_path: Path, prompts: Path, session_stem: str) -> Config:
    """A test-agent Config whose session file is sessions/test-agent/<session_stem>.jsonl."""
    agent_dir = tmp_path / ".js" / "sessions" / "test-agent"
    return Config(
        agent_id="test-agent",
        agent_dir=agent_dir,
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
        history_file=tmp_path / ".history",
        sessions_dir=agent_dir,
        session_file=agent_dir / f"{session_stem}.jsonl",
        prompts_dir=prompts,
    )


def _continue_args(output: str) -> list[str]:
    """The resume command printed after a saved one-shot answer, as argv."""
    last = output.rstrip("\n").splitlines()[-1]
    return shlex.split(last[last.index("js "):])


def test_config_defaults_to_defaultagent_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setenv("JS_DEBUG", "1")

    from js.config import from_env

    actual = from_env()

    # Platformdirs layout: sessions/state live under the platform data dir.
    expected_agent_dir = tmp_path / ".js" / "sessions" / "defaultagent"
    expected_state_dir = tmp_path / ".js" / "state" / "defaultagent"
    assert actual.agent_id == "defaultagent"
    assert actual.agent_dir == expected_agent_dir
    assert actual.history_file == expected_agent_dir / ".history"
    assert actual.session_file.parent == expected_agent_dir
    assert actual.session_file.suffix == ".jsonl"
    assert actual.session_file.exists()
    assert actual.debug_log == expected_state_dir / "debug.log"
    assert actual.prompts_dir.name == "defaultagent"
    assert actual.sessions_dir == expected_agent_dir
    latest = json.loads((expected_agent_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["session_file"] == str(actual.session_file)
    # No jsrc means no file: only /save writes one.
    assert not (tmp_path / ".js" / "jsrc").exists()
def test_personal_defaultagent_overrides_repo_defaultagent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    personal_default = tmp_path / ".js" / "agents" / "defaultagent"
    personal_default.mkdir(parents=True)
    (personal_default / "agent.yaml").write_text("tools: []\n", encoding="utf-8")
    (personal_default / "01-prompt.md").write_text("personal defaultagent\n", encoding="utf-8")

    from js.config import from_env

    actual = from_env(save_session=False)

    assert actual.agent_id == "defaultagent"
    assert actual.prompts_dir == personal_default


def test_config_default_sessions_are_unique_and_latest_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    from js.config import from_env

    configs = [from_env() for _ in range(10)]
    session_files = [cfg.session_file for cfg in configs]

    assert len(set(session_files)) == 10
    for session_file in session_files:
        assert session_file.parent == tmp_path / ".js" / "sessions" / "defaultagent"
        assert session_file.exists()
    latest = json.loads((tmp_path / ".js" / "sessions" / "defaultagent" / "latest.json").read_text(encoding="utf-8"))
    assert latest["session_file"] == str(session_files[-1])


def test_config_rejects_unsafe_agent_id_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JS_AGENT", "../../etc")
    monkeypatch.delenv("JS_SESSION", raising=False)

    from js.config import from_env

    with pytest.raises(ValueError):
        from_env()

    assert not (tmp_path / ".js").exists()


def test_cli_rejects_unsafe_agent_id_argument(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)

    actual = cli.main(["--agent", "../../etc", "-p", "ignored"])

    assert actual == 2
    assert not (tmp_path / ".js").exists()


@pytest.mark.parametrize(
    ("option", "documented_options"),
    [
        ("--help", ["--no-save", "--help-full"]),
        ("--help-full", ["--help-full", "--providers-json", "--printonly"]),
    ],
)
def test_cli_help_exposes_options_and_exits_successfully(option, documented_options, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([option])

    captured = capsys.readouterr()
    assert exc.value.code == 0
    assert captured.err == ""
    for flag in documented_options:
        assert flag in captured.out


def test_compact_command_summarizes_with_the_active_model(monkeypatch, tmp_path):
    from js.config import from_env

    seen: list[str] = []

    def compact_stub(cfg, system, messages, *, focus="", forced=False, **kwargs):
        seen.append(cfg.model)
        return "compacted: fixture"

    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=compact_stub))
    cfg = from_env()
    state, _spec = repl_state(cfg, model="flag-model")

    assert cli._handle_command("/compact", state, cfg) is True
    assert seen == ["flag-model"]


def test_interactive_prompt_enables_ctrl_z_suspend(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    seen = {}

    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            seen.update(kwargs)

        def prompt(self, *_args, **_kwargs):
            raise EOFError

    monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)

    assert cli.main(["--blocking"]) == 0
    assert seen["enable_suspend"] is True


def test_prompt_model_flag_with_provider_prefix_routes_provider_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    # A `-m provider/model` override routes only when that provider is logged in.
    from js import logins
    monkeypatch.setattr(logins, "_CONFIG_DIR_OVERRIDE", tmp_path / "logins")
    logins.save_login(logins.Login(provider_id="openai-codex", provider_api_key="x"))

    seen = {}

    def fake_run_turn(cfg, system, messages, telemetry, trace_override=False, tool_context=None, **kwargs):
        seen["cfg_model"] = cfg.model
        seen["cfg_provider_id"] = cfg.provider_id
        seen["model_override"] = kwargs.get("model_override")
        seen["provider_id_override"] = kwargs.get("provider_id_override")
        messages.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli.runtime, "run_turn", fake_run_turn)
    monkeypatch.setattr(cli.M, "load_messages", lambda _path: [])
    monkeypatch.setattr(cli, "_append_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_args, **_kwargs: None)

    actual = cli._run_prompt("foo", model="openai-codex/gpt-5.5")

    assert actual == 0
    assert seen["cfg_model"] == "gpt-5.5"
    assert seen["cfg_provider_id"] == "openai-codex"
    assert seen["model_override"] == "gpt-5.5"
    assert seen["provider_id_override"] == "openai-codex"


def test_interactive_model_flag_with_provider_prefix_routes_provider_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.delenv("JS_MODEL", raising=False)
    monkeypatch.delenv("JS_PROVIDER", raising=False)
    monkeypatch.delenv("JS_BASE_URL", raising=False)
    monkeypatch.delenv("JS_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # A `--model provider/model` override routes only when that provider is logged in.
    from js import logins
    monkeypatch.setattr(logins, "_CONFIG_DIR_OVERRIDE", tmp_path / "logins")
    logins.save_login(logins.Login(provider_id="openai-codex", provider_api_key="x"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    seen = {}

    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            self.lines = iter(["hi", "exit"])

        def prompt(self, *_args, **_kwargs):
            return next(self.lines)

    def fake_run_turn(cfg, system, messages, telemetry, trace_override=False, tool_context=None, **kwargs):
        seen["cfg_model"] = cfg.model
        seen["cfg_provider_id"] = cfg.provider_id
        seen["model_override"] = kwargs.get("model_override")
        seen["provider_id_override"] = kwargs.get("provider_id_override")
        messages.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", fake_run_turn)
    monkeypatch.setattr(cli, "_append_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_args, **_kwargs: None)

    actual = cli.main(["--blocking", "--model", "openai-codex/gpt-5.5"])

    assert actual == 0
    assert seen["cfg_model"] == "gpt-5.5"
    assert seen["cfg_provider_id"] == "openai-codex"
    assert seen["model_override"] is None
    assert seen["provider_id_override"] is None


def test_cli_rejects_debug_and_debug_file_combination(monkeypatch):
    monkeypatch.setattr(cli, "_run_prompt", lambda *a, **k: pytest.fail("ran the prompt"))

    assert cli.main(["--debug", "--debug-file", "/tmp/js-debug.log", "-p", "ignored"]) == 2


def test_config_existing_session_id_loads_with_and_without_suffix(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    from js.config import from_env

    sessions_dir = tmp_path / ".js" / "sessions" / "defaultagent"
    sessions_dir.mkdir(parents=True)
    existing = sessions_dir / "foo-20260519T010203000000Z-deadbeefcafebabe.jsonl"
    existing.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    monkeypatch.setenv("JS_SESSION", existing.stem)
    without_suffix = from_env()
    monkeypatch.setenv("JS_SESSION", existing.name)
    with_suffix = from_env()

    assert without_suffix.session_file == existing
    assert with_suffix.session_file == existing
    assert list(sessions_dir.glob("foo-*.jsonl")) == [existing]


def test_config_missing_session_id_creates_named_session(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.setenv("JS_SESSION", "foo")

    from js.config import from_env

    sessions_dir = tmp_path / ".js" / "sessions" / "defaultagent"

    actual = from_env()

    assert actual.session_file == sessions_dir / "foo.jsonl"
    assert actual.session_file.is_file()


def test_config_existing_absolute_session_path_loads_exact_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    sessions_dir = tmp_path / ".js" / "sessions" / "defaultagent"
    sessions_dir.mkdir(parents=True)
    existing = sessions_dir / "foo-20260519T010203000000Z-deadbeefcafebabe.jsonl"
    existing.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    monkeypatch.setenv("JS_SESSION", str(existing))

    from js.config import from_env

    actual = from_env()

    assert actual.session_file == existing
    assert list(sessions_dir.glob("foo-*.jsonl")) == [existing]


def test_config_rejects_absolute_session_outside_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    monkeypatch.setenv("JS_SESSION", str(outside))

    from js.config import from_env

    with pytest.raises(ValueError):
        from_env()


def test_js_prompt_mode_persists_turn_for_repl_continuity(monkeypatch, tmp_path, capsys):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    cfg = _prompt_cfg(tmp_path, prompts, "prompt")
    calls: list[dict] = []

    def completion_stub(**kwargs):
        calls.append(kwargs)
        return _fake_stream_result("I can write that scraper.")

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = cli._run_prompt("Can you write a recipe scraper?")

    output = capsys.readouterr().out
    messages = load_messages(cfg.session_file)
    expected = [
        {"role": "user", "content": "Can you write a recipe scraper?"},
        {"role": "assistant", "content": "I can write that scraper."},
    ]
    assert actual == 0
    assert output.splitlines()[0] == "I can write that scraper."
    assert _continue_args(output)[-2:] == ["--session", "prompt"]
    assert messages == expected
    sys_msg = calls[0]["messages"][0]
    assert sys_msg.role == "system"
    assert sys_msg.parts[0].text == "SYSTEM\n"


def test_js_prompt_mode_reads_pipe_without_prompt_flag(monkeypatch):
    calls: list[dict] = []

    class StdinStub:
        def isatty(self):
            return False

        def read(self):
            return "Reply with PIPE_OK"

    monkeypatch.setattr(cli, "_run_prompt", lambda prompt, **kwargs: calls.append({"prompt": prompt, **kwargs}) or 0)
    monkeypatch.setattr(cli.sys, "stdin", StdinStub())

    assert cli.main([]) == 0
    assert [call["prompt"] for call in calls] == ["Reply with PIPE_OK"]
    assert calls[0]["save"] is True


def test_js_prompt_flag_reads_pipe(monkeypatch):
    calls: list[str] = []

    class StdinStub:
        def isatty(self):
            return False

        def read(self):
            return "Reply with PIPE_FLAG_OK"

    monkeypatch.setattr(cli, "_run_prompt", lambda prompt, **kwargs: calls.append(prompt) or 0)
    monkeypatch.setattr(cli.sys, "stdin", StdinStub())

    assert cli.main(["-p"]) == 0
    assert calls == ["Reply with PIPE_FLAG_OK"]


def test_prompt_instruction_combines_with_piped_stdin(monkeypatch):
    calls: list[str] = []

    class StdinStub:
        def isatty(self):
            return False

        def read(self):
            return "diff --git a/file b/file\n+changed\n"

    monkeypatch.setattr(cli, "_run_prompt", lambda prompt, **kwargs: calls.append(prompt) or 0)
    monkeypatch.setattr(cli.sys, "stdin", StdinStub())

    assert cli.main(["-p", "review this patch"]) == 0
    assert calls == ["review this patch\n\ndiff --git a/file b/file\n+changed"]


def test_prompt_model_override_is_preserved_in_continue_hint(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    def completion_stub(**kwargs):
        return _fake_stream_result("MODEL_HINT_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = cli._run_prompt("Reply with MODEL_HINT_OK", model="hint-model")

    output = capsys.readouterr().out
    session_file = next((tmp_path / ".js" / "sessions" / "defaultagent").glob("*.jsonl"))
    assert actual == 0
    assert output.splitlines()[0] == "MODEL_HINT_OK"
    assert _continue_args(output) == ["js", "--model", "hint-model", "--session", session_file.stem]


def test_resumed_prompt_uses_js_model_over_me_model_and_config(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setenv("JS_MODEL", "from-js-model")
    config_dir = tmp_path / ".js"
    config_dir.mkdir(parents=True)
    (config_dir / "jsrc").write_text("set model.id from-config\n", encoding="utf-8")
    session_dir = tmp_path / ".js" / "sessions" / "defaultagent"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "resume-env-model.jsonl"
    cli.M.append_message(session_file, {"role": "user", "content": "old"})
    seen: list[str | None] = []

    def completion_stub(**kwargs):
        seen.append(kwargs.get("model_id"))
        return _fake_stream_result("ENV_MODEL_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = cli._run_prompt("continue", session="resume-env-model")

    output = capsys.readouterr().out
    assert actual == 0
    assert seen == ["from-js-model"]
    assert _continue_args(output) == ["js", "--session", "resume-env-model"]


def test_js_prompt_mode_generated_session_prints_usable_continue_hint(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    def completion_stub(**kwargs):
        return _fake_stream_result("GENERATED_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = cli._run_prompt("Reply with GENERATED_OK")

    captured = capsys.readouterr()
    # New layout: sessions live directly under the per-agent dir.
    agent_dir = tmp_path / ".js" / "sessions" / "defaultagent"
    session_files = list(agent_dir.glob("*.jsonl"))
    assert actual == 0
    assert len(session_files) == 1
    session_file = session_files[0]
    assert captured.out.splitlines()[0] == "GENERATED_OK"
    assert _continue_args(captured.out) == ["js", "--session", session_file.stem]
    assert load_messages(session_file) == [
        {"role": "user", "content": "Reply with GENERATED_OK"},
        {"role": "assistant", "content": "GENERATED_OK"},
    ]
    latest = json.loads((agent_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["session_file"] == str(session_file)


def test_js_prompt_mode_no_save_writes_no_session_or_latest(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    def completion_stub(**kwargs):
        return _fake_stream_result("NO_SAVE_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = cli._run_prompt("Reply with NO_SAVE_OK", save=False)

    captured = capsys.readouterr()
    agent_dir = tmp_path / ".js" / "sessions" / "defaultagent"
    assert actual == 0
    assert captured.out == "NO_SAVE_OK\n"
    assert not (agent_dir / "latest.json").exists()
    assert not list(agent_dir.glob("*.jsonl"))
    assert not (agent_dir / ".no-save.jsonl").exists()


# Agent drivers read this stderr line after a headless run as the signal that
# the next round cannot resume (docs/configuration-and-sessions.md).
NO_SAVE_WARNING = msgs.NOT_SAVED_NO_RESUME.line()


def test_js_pipe_modes_no_save_pass_save_false(monkeypatch, capsys):
    calls: list[dict] = []

    class StdinStub:
        def isatty(self):
            return False

        def read(self):
            return "Reply with PIPE_NO_SAVE_OK"

    monkeypatch.setattr(cli, "_run_prompt", lambda prompt, **kwargs: calls.append({"prompt": prompt, **kwargs}) or 0)
    monkeypatch.setattr(cli.sys, "stdin", StdinStub())

    for argv in (["--no-save"], ["--no-save", "-p"], ["-n", "-p", "hi"]):
        assert cli.main(argv) == 0
        err = capsys.readouterr().err
        assert err.splitlines().count(NO_SAVE_WARNING) == 1
        assert err.endswith(f"{NO_SAVE_WARNING}\n")
    assert [(call["prompt"], call["save"]) for call in calls] == [
        ("Reply with PIPE_NO_SAVE_OK", False),
        ("Reply with PIPE_NO_SAVE_OK", False),
        ("hi\n\nReply with PIPE_NO_SAVE_OK", False),
    ]


def test_no_save_warning_is_only_for_a_headless_no_save_run(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setattr(cli, "_run_prompt", lambda prompt, **kwargs: 0)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            pass

        def prompt(self, *_args, **_kwargs):
            raise EOFError

    monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)

    assert cli.main(["-p", "hi"]) == 0
    assert NO_SAVE_WARNING not in capsys.readouterr().err
    assert cli.main(["--blocking", "-n"]) == 0
    assert NO_SAVE_WARNING not in capsys.readouterr().err


def test_clustered_short_booleans_parse_with_prompt(monkeypatch):
    calls: list[dict] = []

    def run_prompt_stub(prompt, model=None, debug=False, debug_file=None, agent=None, session=None, save=True,
                        reasoning=None, maxout=None, extras=None, **_kwargs):
        calls.append(
            {
                "prompt": prompt,
                "model": model,
                "debug": debug,
                "agent": agent,
                "session": session,
                "save": save,
            }
        )
        return 0

    monkeypatch.setattr(cli, "_run_prompt", run_prompt_stub)

    actual = cli.main(["-nd", "-p", "hi"])

    assert actual == 0
    assert calls == [
        {
            "prompt": "hi",
            "model": None,
            "debug": True,
            "agent": None,
            "session": None,
            "save": False,
        }
    ]


def test_prompt_mode_auto_compact_uses_model_override_for_same(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    seen: list[str] = []

    def run_turn_stub(cfg, system, messages, telemetry, **kwargs):
        monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 120_000, raising=False)
        messages.append({"role": "assistant", "content": "PROMPT_COMPACT_OK"})

    def compact_stub(cfg, system, messages, *, forced=False, **kwargs):
        seen.append(cfg.model)
        return "compacted: fixture"

    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=compact_stub))
    monkeypatch.setattr(cli.runtime, "_resolve_context_window", lambda _model, _provider, _base_url=None: 150_000)

    actual = cli._run_prompt("hi", model="flag-model")

    assert actual == 0
    assert "PROMPT_COMPACT_OK" in capsys.readouterr().out
    assert seen == ["flag-model"]


def test_cli_refresh_model_catalog_flag_exits_after_forced_refresh(monkeypatch):
    seen: list[str] = []

    def refresh_stub() -> bool:
        seen.append("forced")
        return True

    monkeypatch.setattr(cli, "_force_refresh_model_catalog", refresh_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main(["--refresh-model-catalog"])

    assert actual == 0
    assert seen == ["forced"]


def test_prompt_mode_reasoning_off_and_maxout_forward_explicit_overrides(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JS_REASONING", "high")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    cfg = replace(_prompt_cfg(tmp_path, prompts, "knobs"), reasoning_effort="high", max_output_tokens=99)
    seen: dict[str, object] = {}

    def completion_stub(**kwargs):
        seen["reasoning_effort_present"] = "reasoning_effort" in kwargs
        seen["max_output_tokens"] = kwargs["max_output_tokens"]
        return _fake_stream_result("KNOBS_OK")

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = cli._run_prompt("hi", reasoning="off", maxout=321)

    assert actual == 0
    assert capsys.readouterr().out.splitlines()[0] == "KNOBS_OK"
    assert seen == {"reasoning_effort_present": True, "max_output_tokens": 321}


def test_warn_missing_binaries_once_per_binary(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name in {"rg", "fd"} else f"/bin/{name}")
    monkeypatch.setattr(cli, "resolve_binary", lambda name: None if name in {"rg", "fd"} else f"/bin/{name}")
    cli._warned_binaries.clear()

    cli._warn_missing_binaries()
    cli._warn_missing_binaries()

    # One line per missing binary, across both calls.
    assert len(capsys.readouterr().err.splitlines()) == 2
    assert cli._warned_binaries == {"rg", "fd"}
    cli._warned_binaries.clear()


def test_prompt_mode_missing_agent_fails_before_the_provider(monkeypatch, tmp_path, capsys):
    """Finding 55: a nonexistent agent id names the agent and the global agents
    dir where one is created, not the raw missing prompts directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **_k: pytest.fail("provider reached"))

    assert cli._run_prompt("hi", agent="ghostagent") == 2

    err = capsys.readouterr().err
    assert "ghostagent" in err
    assert str(tmp_path / ".js" / "agents") in err


def test_prompt_mode_invalid_reasoning_errors_cleanly_before_provider(monkeypatch, tmp_path, capsys):
    """Ruling B: `--reasoning default` (or any non-ladder token) is rejected with
    rc 2, never shipped verbatim to the provider. The error names the rejected
    token and every accepted value."""
    def explode(**kwargs):
        raise AssertionError("provider must not be reached for an invalid --reasoning")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", explode)

    assert cli._run_prompt("hi", reasoning="default") == 2

    err = capsys.readouterr().err
    assert "default" in err
    for value in settings.REASONING_EFFORT_VALUES:
        assert value in err


def test_bench_mode_invalid_reasoning_errors_cleanly(monkeypatch, tmp_path):
    """The bench loop validates --reasoning up front too (same ruling B path)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    actual = cli._run_bench(
        "someagent", model=None, reasoning="auto", maxout=None, quiet=True, extras=None,
        ignore_local_config=False, ignore_global_config=False, presets=None,
        stats_json=None, stats_csv=None,
    )
    assert actual == 2


def test_offline_compact_model_flag_overrides_same_model(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_dir = tmp_path / ".js" / "sessions" / "defaultagent"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "compact-session.jsonl"
    cli.M.append_message(session_file, {"role": "user", "content": "old"})
    seen: list[str] = []

    def compact_stub(cfg, system, messages, *, focus="", forced=False, **kwargs):
        seen.append(cfg.model)
        return "compacted: fixture"

    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=compact_stub))

    actual = cli._run_compact_offline("compact-session", model="compact-model")

    assert actual == 0
    assert seen == ["compact-model"]


def test_commit_mode_defaults_to_cwd_and_uses_prompt_as_operator_context(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls: list[dict] = []

    def run_prompt_stub(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        return 0

    monkeypatch.setattr(cli, "_run_prompt", run_prompt_stub)

    actual = cli.main(["--commit", "-p", "almost all housekeeping tasks", "-n"])

    assert actual == 0
    prompt = calls[0]["prompt"]
    assert prompt.startswith(f"Commit all work in this target directory: {tmp_path}")
    assert "js.commit_helper" in prompt and "stage" in prompt
    assert msgs.SURVEY_HEADING.text(repo=tmp_path.resolve()) in prompt
    assert "Operator context:\nalmost all housekeeping tasks" in prompt
    assert calls[0]["agent"] == "commit"
    assert calls[0]["save"] is False
    assert calls[0]["resume_prefix"] == f"js --commit {tmp_path}"


def test_commit_mode_accepts_target_dir_and_pipe_context(monkeypatch, tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    calls: list[dict] = []

    class StdinStub:
        def isatty(self):
            return False

        def read(self):
            return "mostly docs cleanup\n"

    def run_prompt_stub(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        return 0

    monkeypatch.setattr(cli, "_run_prompt", run_prompt_stub)
    monkeypatch.setattr(cli.sys, "stdin", StdinStub())

    actual = cli.main(["--commit", str(target), "-p", "-"])

    assert actual == 0
    prompt = calls[0]["prompt"]
    assert prompt.startswith(f"Commit all work in this target directory: {target}")
    assert "js.commit_helper" in prompt
    assert "Operator context:\nmostly docs cleanup" in prompt
    assert calls[0]["agent"] == "commit"


def test_commit_mode_rejects_agent_override_and_missing_target(monkeypatch, tmp_path):
    missing = tmp_path / "missing"
    monkeypatch.setattr(cli, "_run_prompt", lambda *a, **k: pytest.fail("ran the commit agent"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    assert cli.main(["--commit", "--agent", "autocoder"]) == 2
    assert cli.main(["--commit", str(missing)]) == 2


def test_resumed_prompt_model_override_is_used_and_preserved_in_continue_hint(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    session_dir = tmp_path / ".js" / "sessions" / "defaultagent"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "resume-model.jsonl"
    cli.M.append_message(session_file, {"role": "user", "content": "old"})
    seen: list[str | None] = []

    def run_turn_stub(cfg, system, messages, telemetry, **kwargs):
        seen.append(kwargs.get("model_override"))
        messages.append({"role": "assistant", "content": "RESUME_MODEL_OK"})

    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)

    actual = cli._run_prompt("continue", session="resume-model", model="resume-model-override")

    output = capsys.readouterr().out
    assert actual == 0
    assert seen == ["resume-model-override"]
    assert _continue_args(output) == ["js", "--model", "resume-model-override", "--session", "resume-model"]


def test_short_session_and_agent_aliases_parse(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(cli, "_run_prompt", lambda prompt, **kwargs: calls.append(kwargs) or 0)

    assert cli.main(["-a", "scoped", "-s", "short-session", "-p", "hi"]) == 0
    assert (calls[0]["agent"], calls[0]["session"]) == ("scoped", "short-session")


def test_agent_scopes_session_lookup(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    # Sessions live directly under the platform data sessions/<agent>/ dir.
    scoped_dir = tmp_path / ".js" / "sessions" / "scoped"
    default_dir = tmp_path / ".js" / "sessions" / "defaultagent"
    scoped_dir.mkdir(parents=True)
    default_dir.mkdir(parents=True)
    session_name = "scoped-session.jsonl"
    scoped_session = scoped_dir / session_name
    default_session = default_dir / session_name
    cli.M.append_message(scoped_session, {"role": "user", "content": "scoped old"})
    cli.M.append_message(default_session, {"role": "user", "content": "default old"})
    loaded_prompt_dirs = []

    def completion_stub(**kwargs):
        return _fake_stream_result("SCOPED_SESSION_OK")

    def load_prompt_spec_stub(prompts_dir):
        loaded_prompt_dirs.append(prompts_dir)
        return cli.P.PromptSpec(system="SYSTEM\n", tool_selectors=())

    monkeypatch.setattr(cli.P, "load_prompt_spec", load_prompt_spec_stub)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = cli._run_prompt("Reply with SCOPED_SESSION_OK", agent="scoped", session="scoped-session")

    output = capsys.readouterr().out
    assert actual == 0
    # The resume hint must name the agent: the session lives under
    # sessions/scoped, so an agent-less `js --session ...` would resolve against
    # sessions/defaultagent and 404 the .jsonl.
    assert _continue_args(output) == ["js", "--agent", "scoped", "--session", "scoped-session"]
    assert loaded_prompt_dirs[0].name == "scoped"
    assert load_messages(scoped_session) == [
        {"role": "user", "content": "scoped old"},
        {"role": "user", "content": "Reply with SCOPED_SESSION_OK"},
        {"role": "assistant", "content": "SCOPED_SESSION_OK"},
    ]
    assert load_messages(default_session) == [{"role": "user", "content": "default old"}]


def _auto_compact_cfg(tmp_path, *, compact: dict | None = None) -> Config:
    return Config(
        agent_id="auto",
        agent_dir=tmp_path / ".js" / "sessions" / "auto",
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
        history_file=tmp_path / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / "auto",
        session_file=tmp_path / ".js" / "sessions" / "auto" / "auto.jsonl",
        prompts_dir=tmp_path / "prompts",
        # buffer_tokens 0 keeps these threshold tests on raw-window math, so the
        # synthetic 100-token window still means "80 tokens == 80% full"; the
        # reserve/buffer subtraction itself is covered separately below.
        settings={"compact": {"buffer_tokens": 0, **({"context_window": 100} if not (compact or {}).get("context_window_fallback") else {}), **(compact or {})}},
    )


def _auto_state() -> dict:
    return {
        "system": "SYSTEM",
        "messages": [{"role": "user", "content": "hi"}],
        "auto_compact": cli.compaction.AutoCompactState(),
    }


def test_auto_compact_noops_when_disabled_or_paused(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 95, raising=False)
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))

    disabled = _auto_compact_cfg(tmp_path, compact={"auto": False})
    cli._maybe_auto_compact(disabled, _auto_state())
    paused_state = _auto_state()
    paused_state["auto_compact"].paused = True
    cli._maybe_auto_compact(_auto_compact_cfg(tmp_path), paused_state)

    assert calls == []
    assert capsys.readouterr().out == ""


def test_auto_compact_notifies_once_at_threshold_and_resets_below(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))
    cfg = _auto_compact_cfg(tmp_path)
    state = _auto_state()

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 49, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 50, raising=False)
    cli._maybe_auto_compact(cfg, state)
    first = capsys.readouterr().out
    cli._maybe_auto_compact(cfg, state)
    second = capsys.readouterr().out
    assert msgs.AUTO_COMPACT_ARMED.line(fullness=0.50) in first
    assert second == ""
    assert calls == []

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 40, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert state["auto_compact"].notified is False
    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 50, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert msgs.AUTO_COMPACT_ARMED.line(fullness=0.50) in capsys.readouterr().out


def test_auto_compact_uses_active_model_for_same(monkeypatch, tmp_path):
    seen: list[str] = []

    def compact_stub(cfg, system, messages, *, forced=False, **kwargs):
        seen.append(cfg.model)
        return "compacted: fixture"

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 80, raising=False)
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=compact_stub))
    cfg = _auto_compact_cfg(tmp_path)
    state = _auto_state()
    state["model"] = "active-model"

    cli._maybe_auto_compact(cfg, state)

    assert seen == ["active-model"]


def test_auto_compact_triggers_at_80_and_forces_at_90(monkeypatch, tmp_path):
    calls: list[dict] = []

    def compact_stub(cfg, system, messages, *, forced=False, **kwargs):
        calls.append({"forced": forced, "system": system, "messages": messages})
        return "compacted: fixture"

    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=compact_stub))
    cfg = _auto_compact_cfg(tmp_path)

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 80, raising=False)
    cli._maybe_auto_compact(cfg, _auto_state())
    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 90, raising=False)
    cli._maybe_auto_compact(cfg, _auto_state())

    assert [call["forced"] for call in calls] == [False, True]


def test_auto_compact_measures_fullness_after_truncated_replies(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))
    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 10, raising=False)
    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_incomplete_reason", "max_output_tokens", raising=False)
    cfg = _auto_compact_cfg(tmp_path)
    state = _auto_state()
    for _ in range(3):
        cli._maybe_auto_compact(cfg, state)
    assert calls == []
    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 95, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert len(calls) == 1
    assert calls[0]["forced"] is True


def test_auto_compact_pauses_after_two_consecutive_fires_and_resets_below_trigger(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))
    cfg = _auto_compact_cfg(tmp_path)
    state = _auto_state()

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 80, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert state["auto_compact"].consecutive == 1
    assert state["auto_compact"].paused is False
    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 80)
    cli._maybe_auto_compact(cfg, state)
    assert state["auto_compact"].consecutive == 2
    assert state["auto_compact"].paused is True
    cli._maybe_auto_compact(cfg, state)
    assert len(calls) == 2

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 79, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert state["auto_compact"].consecutive == 0
    assert state["auto_compact"].paused is False
    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 80, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert len(calls) == 3


def test_auto_compact_invalid_numeric_config_falls_back_to_defaults(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))

    for compact in (
        {
            "context_window": "not-an-int",
            "notify_threshold": "not-a-float",
            "trigger_threshold": "not-a-float",
            "force_threshold": "not-a-float",
        },
        {
            "context_window": True,
            "notify_threshold": True,
            "trigger_threshold": True,
            "force_threshold": True,
        },
    ):
        cfg = _auto_compact_cfg(tmp_path, compact=compact)
        monkeypatch.setattr(cli.runtime, "_resolve_context_window", lambda _model, _provider, _base_url=None: 131072)
        monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 104858, raising=False)  # ~80% of mocked metadata window
        cli._maybe_auto_compact(cfg, _auto_state())

    assert len(calls) == 2
    assert [call["forced"] for call in calls] == [False, False]
    assert capsys.readouterr().out.count(msgs.AUTO_COMPACT_ARMED.line(fullness=0.80)) == 2


def test_auto_compact_misordered_thresholds_use_safe_defaults(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))
    cfg = _auto_compact_cfg(
        tmp_path,
        compact={
            "notify_threshold": 0.95,  # invalid: notify after trigger
            "trigger_threshold": 0.80,
            "force_threshold": 0.70,   # invalid: force before trigger
        },
    )

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 80, raising=False)
    cli._maybe_auto_compact(cfg, _auto_state())

    assert len(calls) == 1
    assert calls[0]["forced"] is False
    assert msgs.AUTO_COMPACT_ARMED.line(fullness=0.80) in capsys.readouterr().out


def test_auto_compact_string_false_values_disable_auto(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 95, raising=False)
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))

    for raw in ("false", "0", "off", "no"):
        cli._maybe_auto_compact(_auto_compact_cfg(tmp_path, compact={"auto": raw}), _auto_state())

    assert calls == []
    assert capsys.readouterr().out == ""

def test_dash_C_binds_working_dir_for_prompt_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()

    seen: dict[str, str] = {}

    def completion_stub(**kwargs):
        seen["cwd"] = os.getcwd()
        seen["ctx_cwd"] = str(runtime.T.STOCK_CONTEXT.cwd)
        return _fake_stream_result("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    orig = os.getcwd()
    try:
        actual = cli.main(["-C", str(scaffold), "-p", "hi", "-n", "-q"])
    finally:
        os.chdir(orig)

    assert actual == 0
    # git -C semantics: the agent's process cwd AND the tool context it runs
    # against are both bound to the -C dir, so relative paths resolve there.
    assert Path(seen["cwd"]).resolve() == scaffold.resolve()
    assert Path(seen["ctx_cwd"]).resolve() == scaffold.resolve()


def test_dash_C_rejects_missing_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_run_prompt", lambda *a, **k: pytest.fail("ran the prompt"))
    assert cli.main(["-C", str(tmp_path / "nope"), "-p", "hi"]) == 2


def test_auto_compact_fullness_excludes_output_reserve_and_buffer(monkeypatch, tmp_path, capsys):
    # 100k window, 8k reserved for the reply, 4k compaction buffer -> 88k of
    # addressable input. 79k is 79% of the raw window (under the 0.80 trigger)
    # but ~90% of what can actually be filled, so compaction must fire. Before
    # this, the between-turn trigger measured against the raw window and
    # disagreed with the in-turn budget check.
    calls: list[dict] = []
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))
    cfg = _auto_compact_cfg(
        tmp_path,
        compact={"context_window": 100_000, "buffer_tokens": 4_000},
    )
    cfg = replace(cfg, max_output_tokens=8_000)

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 79_000, raising=False)
    cli._maybe_auto_compact(cfg, _auto_state())

    assert len(calls) == 1
    assert calls[0]["forced"] is False
    assert msgs.AUTO_COMPACT_ARMED.line(fullness=0.90) in capsys.readouterr().out


def test_auto_compact_reserve_never_eats_more_than_half_the_window(monkeypatch, tmp_path, capsys):
    # A model declaring a 64k output cap against a 32k window would leave a
    # negative budget; the floor keeps half the window addressable instead of
    # compacting on every single turn.
    calls: list[dict] = []
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))
    cfg = _auto_compact_cfg(
        tmp_path,
        compact={"context_window": 32_000, "buffer_tokens": 4_000},
    )
    cfg = replace(cfg, max_output_tokens=64_000)

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 8_000, raising=False)
    cli._maybe_auto_compact(cfg, _auto_state())

    assert calls == []
    assert msgs.AUTO_COMPACT_ARMED.line(fullness=0.50) in capsys.readouterr().out


def test_reply_reserve_is_capped_so_a_huge_output_limit_does_not_eat_the_window(monkeypatch, tmp_path, capsys):
    # gpt-5.6-sol declares max_output 128000. Reserving all of it against a
    # 370k window would leave 238k addressable and compact at ~190k; the
    # reserve is capped at compact.summary_reserve_tokens (20k default), so
    # 345,904 is addressable and 260k reads as 75%, under the trigger.
    calls: list[dict] = []
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))
    cfg = _auto_compact_cfg(
        tmp_path,
        compact={"context_window": 370_000, "buffer_tokens": 4_096},
    )
    cfg = replace(cfg, max_output_tokens=128_000)

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 260_000, raising=False)
    cli._maybe_auto_compact(cfg, _auto_state())

    assert calls == []
    assert msgs.AUTO_COMPACT_ARMED.line(fullness=0.75) in capsys.readouterr().out


def test_reply_reserve_cap_is_configurable(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))
    cfg = _auto_compact_cfg(
        tmp_path,
        compact={
            "context_window": 370_000,
            "buffer_tokens": 4_096,
            "summary_reserve_tokens": 128_000,
        },
    )
    cfg = replace(cfg, max_output_tokens=128_000)

    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 260_000, raising=False)
    cli._maybe_auto_compact(cfg, _auto_state())

    # Same 260k now measured against 237,904 -> over 100%, compaction fires.
    assert len(calls) == 1


def test_context_window_fallback_only_applies_when_the_model_is_unknown(monkeypatch, tmp_path, capsys):
    # Known model: metadata wins, the fallback is ignored entirely.
    calls: list[dict] = []
    monkeypatch.setattr(cli.compaction, "compact_now", AsyncMock(side_effect=lambda *a, **kw: calls.append(kw) or "compacted: fixture"))
    monkeypatch.setattr(cli.runtime, "_resolve_context_window", lambda *a, **kw: 1_050_000)
    cfg = _auto_compact_cfg(
        tmp_path,
        compact={"context_window_fallback": 370_000, "buffer_tokens": 4_096},
    )
    cfg = replace(cfg, max_output_tokens=128_000)
    # 300k is 81% of a 370k pin but 29% of the real 1.05M window.
    monkeypatch.setattr(cli.runtime.T.STOCK_CONTEXT, "last_prompt_tokens", 300_000, raising=False)
    cli._maybe_auto_compact(cfg, _auto_state())
    assert calls == []

    # Unknown model: nothing to resolve, so the fallback is what we measure by.
    monkeypatch.setattr(cli.runtime, "_resolve_context_window", lambda *a, **kw: None)
    cli._maybe_auto_compact(cfg, _auto_state())
    assert len(calls) == 1


def test_context_window_override_beats_catalog_for_a_specific_surface():
    # models.dev has one row per model id, not per surface: openai/gpt-5.6-sol
    # is 1.05M, but the codex subscription endpoint serving that same id is not
    # in the catalog at all. provider/model must win over provider-agnostic.
    try:
        runtime.set_context_window_overrides({
            "openai-codex/gpt-5.6-sol": 370_000,
            "gpt-5.6-sol": 999_000,
        })
        assert runtime._resolve_context_window("gpt-5.6-sol", "openai-codex", None) == 370_000
        assert runtime._resolve_context_window("gpt-5.6-sol", "openrouter", None) == 999_000
        # Untouched models still come from the catalog.
        assert runtime._resolve_context_window("gpt-5.6-terra", "openai-codex", None) != 370_000
    finally:
        runtime.set_context_window_overrides(None)


def test_context_window_overrides_ignore_junk_entries():
    try:
        runtime.set_context_window_overrides({"a/b": "nope", "c/d": 0, "e/f": -5, "g/h": "7000"})
        assert runtime._resolve_context_window("b", "a", None) is None
        assert runtime._resolve_context_window("d", "c", None) is None
        assert runtime._resolve_context_window("f", "e", None) is None
        assert runtime._resolve_context_window("h", "g", None) == 7000
    finally:
        runtime.set_context_window_overrides(None)


def test_context_window_overrides_rebuild_dotted_model_ids():
    # `set compact.context_window_overrides.openai-codex/gpt-5.6-sol 370000`
    # splits on every dot, so the loader must rejoin the id.
    try:
        runtime.set_context_window_overrides(
            {"openai-codex/gpt-5": {"6-sol": 370_000, "6-terra": 370_000}}
        )
        assert runtime._resolve_context_window("gpt-5.6-sol", "openai-codex", None) == 370_000
        assert runtime._resolve_context_window("gpt-5.6-terra", "openai-codex", None) == 370_000
    finally:
        runtime.set_context_window_overrides(None)


def test_model_context_window_overrides_the_catalog_for_the_active_model(tmp_path):
    # The single-model form, symmetric with model.max_output_tokens.
    cfg = _auto_compact_cfg(tmp_path)
    cfg = replace(cfg, model="gpt-5.6-sol", provider_id="openai-codex", model_context_window=370_000)
    try:
        runtime.install_context_window_overrides(cfg)
        assert runtime._resolve_context_window("gpt-5.6-sol", "openai-codex", None) == 370_000
        # Scoped to the model it names; a sibling still comes from the catalog.
        assert runtime._resolve_context_window("gpt-5.6-terra", "openai-codex", None) != 370_000
    finally:
        runtime.set_context_window_overrides(None)


def test_model_context_window_beats_the_multi_model_map(tmp_path):
    cfg = _auto_compact_cfg(tmp_path)
    cfg = replace(cfg, model="gpt-5.6-sol", provider_id="openai-codex", model_context_window=370_000)
    cfg.settings["compact"]["context_window_overrides"] = {"openai-codex/gpt-5": {"6-sol": 900_000}}
    try:
        runtime.install_context_window_overrides(cfg)
        assert runtime._resolve_context_window("gpt-5.6-sol", "openai-codex", None) == 370_000
    finally:
        runtime.set_context_window_overrides(None)


def test_named_nested_sessions_append_stably(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **_kwargs: _fake_stream_result("OK"))

    assert cli._run_prompt("first", session="caller/nested", show_continue=False) == 0
    assert cli._run_prompt("second", session="caller/nested", show_continue=False) == 0
    nested = tmp_path / ".js" / "sessions" / "defaultagent" / "caller" / "nested.jsonl"
    assert [message["content"] for message in load_messages(nested)] == ["first", "OK", "second", "OK"]


def test_session_key_resumes_the_same_derived_session(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **_kwargs: _fake_stream_result("OK"))
    project = tmp_path / "project"
    project.mkdir()

    assert cli.main(["-C", str(project), "--session-key", "job-7", "-p", "third"]) == 0
    assert cli.main(["-C", str(project), "--session-key", "job-7", "-p", "fourth"]) == 0
    derived = list((tmp_path / ".js" / "sessions" / "defaultagent" / "derived").glob("*.jsonl"))
    assert len(derived) == 1
    assert [message["content"] for message in load_messages(derived[0])] == ["third", "OK", "fourth", "OK"]
    capsys.readouterr()


def test_generated_prompt_emits_machine_session_metadata_even_when_quiet(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **_kwargs: _fake_stream_result("ANSWER"))

    assert cli._run_prompt("hello", show_continue=False) == 0

    captured = capsys.readouterr()
    machine_lines = [json.loads(line) for line in captured.err.splitlines() if line.startswith("{")]
    assert captured.out == "ANSWER\n"
    assert len(machine_lines) == 1
    metadata = machine_lines[0]["js_session"]
    assert metadata["agent"] == "defaultagent"
    assert Path(metadata["path"]).is_absolute()
    assert Path(metadata["path"]).stem == metadata["session_id"]


def test_list_table_and_jsonl_cover_same_nested_records_without_config(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".js" / "sessions"
    old = root / "old" / "legacy.jsonl"
    nested = root / "agent" / "caller" / "nested.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    cli.M.append_message(nested, {"role": "user", "content": "new"})
    from js.session_catalog import record_session_start
    record_session_start(nested, cwd=tmp_path, caller_key="job-key", job_id=9)
    monkeypatch.setattr(cli, "_cfg_from_env_compat", lambda *_args, **_kwargs: pytest.fail("list loaded config"))

    assert cli._print_session_list(json_lines=False) == 0
    table = capsys.readouterr().out
    assert "legacy" in table and "caller/nested" in table
    assert "job-key" in table and str(tmp_path) in table

    assert cli._print_session_list(json_lines=True) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {(item["agent"], item["name"]) for item in records} == {
        ("old", "legacy"),
        ("agent", "caller/nested"),
    }
    assert next(item for item in records if item["agent"] == "old")["user_turns"] == 1
    expected_fields = {"agent", "name", "path", "mtime", "size", "user_turns", "in_flight", "cwd", "caller_key", "job_id", "model"}
    assert all(set(item) == expected_fields for item in records)


def test_list_flag_prints_the_session_list(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(cli, "_print_session_list", lambda *, json_lines: calls.append(json_lines) or 0)

    assert cli.main(["--list"]) == 0
    assert cli.main(["--list", "--json"]) == 0
    assert calls == [False, True]


def test_list_reports_subprocess_session_live_only_while_process_alive(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    session = tmp_path / ".js" / "sessions" / "agent" / "open.jsonl"
    session.parent.mkdir(parents=True)
    session.touch()
    ready = tmp_path / "ready"
    code = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from js.session_catalog import acquire_session\n"
        "acquire_session(Path(sys.argv[1]))\n"
        "Path(sys.argv[2]).touch()\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", code, str(session), str(ready)])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert cli._print_session_list(json_lines=True) == 0
        assert json.loads(capsys.readouterr().out)["in_flight"] is True
    finally:
        process.terminate()
        process.wait(timeout=5)

    assert cli._print_session_list(json_lines=True) == 0
    assert json.loads(capsys.readouterr().out)["in_flight"] is False


def test_json_is_scoped_to_list(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    assert cli.main(["--json"]) == 2
    with pytest.raises(SystemExit):
        cli.main(["-s", "named", "--session-key", "key", "-p", "nope"])


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("failure, status", [(KeyboardInterrupt, 130), (RuntimeError, 1)])
def test_prompt_failure_preserves_tool_work_and_resumes(monkeypatch, tmp_path, debug, failure, status):
    monkeypatch.setenv("HOME", str(tmp_path))
    session = tmp_path / ".js/sessions/defaultagent/interrupted.jsonl"
    user = {"role": "user", "content": "inspect tests"}
    exchange = [
        {"role": "assistant", "content": "checking", "tool_calls": [
            {"id": "read-1", "type": "function", "function": {
                "name": "read", "arguments": '{"path":"justfile"}',
            }},
            {"id": "read-2", "type": "function", "function": {
                "name": "read", "arguments": '{"path":"pyproject.toml"}',
            }},
        ]},
        {"role": "tool", "tool_call_id": "read-1", "name": "read", "content": "test recipe"},
    ]

    def interrupted(cfg, system, messages, telemetry, **kwargs):
        assert load_messages(session) == [user]
        messages.extend(exchange)
        raise failure()

    monkeypatch.setattr(runtime, "run_turn", interrupted)
    assert cli._run_prompt("inspect tests", session="interrupted", debug=debug) == status
    kept = load_messages(session)
    assert kept[:3] == [user, *exchange]
    assert kept[3]["tool_call_id"] == "read-2"

    def resumed(cfg, system, messages, telemetry, **kwargs):
        assert messages == [*kept, {"role": "user", "content": "continue"}]
        messages.append({"role": "assistant", "content": "finished"})

    monkeypatch.setattr(runtime, "run_turn", resumed)
    assert cli._run_prompt("continue", session="interrupted") == 0
    assert load_messages(session) == [
        *kept, {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "finished"},
    ]


def test_prompt_interrupt_keeps_streamed_partial(monkeypatch, tmp_path):
    import asyncio

    async def interrupted(**kwargs):
        kwargs["on_text"]("partial answer")
        raise asyncio.CancelledError()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(runtime.model_client, "stream_model_async", interrupted)
    assert cli._run_prompt("explain", session="partial") == 130
    session = tmp_path / ".js/sessions/defaultagent/partial.jsonl"
    assert load_messages(session) == [
        {"role": "user", "content": "explain"},
        {"role": "assistant", "content": "partial answer", "incomplete_reason": "cancelled"},
    ]


def test_prompt_interrupt_without_save(monkeypatch, tmp_path):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(runtime, "run_turn", interrupted)
    assert cli._run_prompt("explain", save=False) == 130
    assert not list((tmp_path / ".js/sessions").rglob("*.jsonl"))
