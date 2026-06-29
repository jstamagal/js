from __future__ import annotations

import json
import os
from pathlib import Path
import pytest

from js import cli, runtime
from js.config import Config
from js.memory import load_messages
from js.toolkit.registry import build_default_registry
from js.model_client import ModelStreamResult

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

def test_config_defaults_to_defaultagent_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setenv("JS_DEBUG", "1")

    from js.config import from_env

    actual = from_env()

    # Platformdirs layout: sessions/state live under the platform data dir.
    expected_agent_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    expected_state_dir = tmp_path / ".local" / "share" / "js" / "state" / "defaultagent"
    assert actual.agent_id == "defaultagent"
    assert actual.agent_dir == expected_agent_dir
    assert actual.history_file == expected_agent_dir / ".history"
    assert actual.session_file.parent == expected_agent_dir
    assert actual.session_file.name != "conversation.jsonl"
    assert actual.session_file.suffix == ".jsonl"
    assert actual.session_file.exists()
    assert actual.debug_log == expected_state_dir / "debug.log"
    assert actual.prompts_dir.name == "defaultagent"
    assert actual.sessions_dir == expected_agent_dir
    latest = json.loads((expected_agent_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["session_file"] == str(actual.session_file)
    # First-run template is written to the platform config dir.
    assert (tmp_path / ".config" / "js" / "jsrc").exists()

    assert not hasattr(actual, "memory_file")


def test_personal_defaultagent_overrides_repo_defaultagent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    personal_default = tmp_path / ".config" / "js" / "agents" / "defaultagent"
    personal_default.mkdir(parents=True)
    (personal_default / "00-tools.md").write_text("---\ntools: []\n---\n", encoding="utf-8")
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
        assert session_file.parent == tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
        assert session_file.name != "conversation.jsonl"
        assert session_file.exists()
    latest = json.loads((tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent" / "latest.json").read_text(encoding="utf-8"))
    assert latest["session_file"] == str(session_files[-1])


def test_config_rejects_unsafe_agent_id_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JS_AGENT", "../../etc")
    monkeypatch.delenv("JS_SESSION", raising=False)

    from js.config import from_env

    with pytest.raises(ValueError, match="agent id"):
        from_env()

    assert not (tmp_path / ".js").exists()


def test_cli_rejects_unsafe_agent_id_argument(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)

    actual = cli.main(["--agent", "../../etc", "-p", "ignored"])

    captured = capsys.readouterr()
    assert actual == 2
    assert "agent id" in captured.err
    assert not (tmp_path / ".js").exists()


def test_cli_help_describes_effective_model_precedence(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])

    captured = capsys.readouterr()
    assert exc.value.code == 0
    assert "override configured/env model" in captured.out
    assert "override JS_MODEL" not in captured.out
    assert "Wins over" in captured.out
    assert "all config files" in captured.out
    assert "minimal" in captured.out
    assert "min=low" in captured.out
    assert "platform data" in captured.out
    assert "sessions/<agent>" in captured.out
    assert "state/<agent>" in captured.out
    assert "~/.js" not in captured.out


def test_interactive_compact_uses_active_model_for_same(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    seen: list[str] = []

    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            self.lines = iter(["/compact", "exit"])

        def prompt(self, *_args, **_kwargs):
            return next(self.lines)

    def compact_stub(cfg, system, messages, *, focus="", forced=False):
        seen.append(cfg.model)
        return "compacted"

    monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)
    monkeypatch.setattr(cli.runtime, "compact_messages", compact_stub)

    actual = cli.main(["--model", "flag-model"])

    assert actual == 0
    assert seen == ["flag-model"]


def test_interactive_cli_model_flag_overrides_banner_model(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            pass

        def prompt(self, *_args, **_kwargs):
            raise EOFError

    monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)

    actual = cli.main(["--model", "flag-model"])

    output = capsys.readouterr().out

    assert actual == 0
    assert "flag-model" in output
    assert "deepseek/deepseek-v4-flash" not in output


def test_prompt_model_flag_with_provider_prefix_routes_provider_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

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

    actual = cli.main(["-p", "foo", "-m", "openai-codex/gpt-5.5"])

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

    actual = cli.main(["--model", "openai-codex/gpt-5.5"])

    assert actual == 0
    assert seen["cfg_model"] == "gpt-5.5"
    assert seen["cfg_provider_id"] == "openai-codex"
    assert seen["model_override"] is None
    assert seen["provider_id_override"] is None


def test_cli_rejects_debug_and_debug_file_combination(capsys):
    actual = cli.main(["--debug", "--debug-file", "/tmp/js-debug.log", "-p", "ignored"])

    captured = capsys.readouterr()
    assert actual == 2
    assert "either --debug or --debug-file" in captured.err


def test_config_existing_session_id_loads_with_and_without_suffix(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    from js.config import from_env

    sessions_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
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


def test_config_missing_session_id_errors_and_creates_no_matching_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.setenv("JS_SESSION", "foo")

    from js.config import from_env

    sessions_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"

    with pytest.raises(ValueError, match="existing"):
        from_env()

    assert not list(sessions_dir.glob("foo*.jsonl"))


def test_config_existing_absolute_session_path_loads_exact_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    sessions_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    sessions_dir.mkdir(parents=True)
    existing = sessions_dir / "foo-20260519T010203000000Z-deadbeefcafebabe.jsonl"
    existing.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    monkeypatch.setenv("JS_SESSION", str(existing))

    from js.config import from_env

    actual = from_env()

    assert actual.session_file == existing
    assert actual.session_file == existing
    assert list(sessions_dir.glob("foo-*.jsonl")) == [existing]


def test_config_rejects_absolute_session_outside_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    monkeypatch.setenv("JS_SESSION", str(outside))

    from js.config import from_env

    with pytest.raises(ValueError, match="inside"):
        from_env()


def test_js_prompt_mode_persists_turn_for_repl_continuity(monkeypatch, tmp_path, capsys):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    cfg = Config(
        agent_id="test-agent",
        agent_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent",
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
        sessions_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent",
        session_file=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent" / "prompt.jsonl",
        prompts_dir=prompts,
    )
    calls: list[dict] = []

    def completion_stub(**kwargs):
        calls.append(kwargs)
        return _fake_stream_result("I can write that scraper.")

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["-p", "Can you write a recipe scraper?"])

    output = capsys.readouterr().out
    messages = load_messages(cfg.session_file)
    expected = [
        {"role": "user", "content": "Can you write a recipe scraper?"},
        {"role": "assistant", "content": "I can write that scraper."},
    ]
    assert actual == 0
    assert output == "I can write that scraper.\nContinue: js --session prompt\n"
    assert messages == expected
    sys_msg = calls[0]["messages"][0]
    assert sys_msg.role == "system"
    assert sys_msg.parts[0].text == "SYSTEM\n"


def test_js_prompt_mode_reads_pipe_without_prompt_flag(monkeypatch, tmp_path, capsys):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    cfg = Config(
        agent_id="test-agent",
        agent_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent",
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
        sessions_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent",
        session_file=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent" / "pipe.jsonl",
        prompts_dir=prompts,
    )

    def completion_stub(**kwargs):
        return _fake_stream_result("PIPE_OK")

    class StdinStub:
        def isatty(self):
            return False

        def read(self):
            return "Reply with PIPE_OK"

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)
    monkeypatch.setattr(cli.sys, "stdin", StdinStub())

    actual = cli.main([])

    output = capsys.readouterr().out
    assert actual == 0
    assert output == "PIPE_OK\nContinue: js --session pipe\n"
    assert load_messages(cfg.session_file)[0] == {"role": "user", "content": "Reply with PIPE_OK"}


def test_js_prompt_flag_reads_pipe(monkeypatch, tmp_path, capsys):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    cfg = Config(
        agent_id="test-agent",
        agent_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent",
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
        sessions_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent",
        session_file=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent" / "pipe-flag.jsonl",
        prompts_dir=prompts,
    )

    def completion_stub(**kwargs):
        return _fake_stream_result("PIPE_FLAG_OK")

    class StdinStub:
        def isatty(self):
            return False

        def read(self):
            return "Reply with PIPE_FLAG_OK"

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)
    monkeypatch.setattr(cli.sys, "stdin", StdinStub())

    actual = cli.main(["-p"])

    output = capsys.readouterr().out
    expected = [
        {"role": "user", "content": "Reply with PIPE_FLAG_OK"},
        {"role": "assistant", "content": "PIPE_FLAG_OK"},
    ]
    assert actual == 0
    assert output == "PIPE_FLAG_OK\nContinue: js --session pipe-flag\n"
    assert load_messages(cfg.session_file) == expected


def test_prompt_instruction_combines_with_piped_stdin(monkeypatch, tmp_path, capsys):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    cfg = Config(
        agent_id="test-agent",
        agent_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent",
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
        sessions_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent",
        session_file=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent" / "pipe-review.jsonl",
        prompts_dir=prompts,
    )
    seen: list[str] = []

    def completion_stub(**kwargs):
        seen.append(kwargs["messages"][-1].parts[0].text)
        return _fake_stream_result("REVIEW_OK")

    class StdinStub:
        def isatty(self):
            return False

        def read(self):
            return "diff --git a/file b/file\n+changed\n"

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)
    monkeypatch.setattr(cli.sys, "stdin", StdinStub())

    actual = cli.main(["-p", "review this patch"])

    output = capsys.readouterr().out
    assert actual == 0
    assert output == "REVIEW_OK\nContinue: js --session pipe-review\n"
    assert seen == ["review this patch\n\ndiff --git a/file b/file\n+changed"]
    assert load_messages(cfg.session_file)[0] == {
        "role": "user",
        "content": "review this patch\n\ndiff --git a/file b/file\n+changed",
    }


def test_js_prompt_existing_session_persists_to_selected_session(monkeypatch, tmp_path, capsys):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    # New layout: sessions live directly under the per-agent dir.
    agent_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent"
    session_file = agent_dir / "2026-05-18-soup-20260519T010203000000Z-deadbeefcafebabe.jsonl"
    cfg = Config(
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
        history_file=agent_dir / ".history",
        sessions_dir=agent_dir,
        session_file=session_file,
        prompts_dir=prompts,
    )

    def completion_stub(**kwargs):
        return _fake_stream_result("NAMED_SESSION_OK")

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["--session", session_file.stem, "-p", "Reply with NAMED_SESSION_OK"])

    output = capsys.readouterr().out
    expected = [
        {"role": "user", "content": "Reply with NAMED_SESSION_OK"},
        {"role": "assistant", "content": "NAMED_SESSION_OK"},
    ]
    assert session_file.name.startswith("2026-05-18-soup-")
    assert actual == 0
    assert output == f"NAMED_SESSION_OK\nContinue: js --session {session_file.stem}\n"
    assert load_messages(session_file) == expected


def test_prompt_model_override_is_preserved_in_continue_hint(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    def completion_stub(**kwargs):
        return _fake_stream_result("MODEL_HINT_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["--model", "hint-model", "-p", "Reply with MODEL_HINT_OK"])

    output = capsys.readouterr().out
    session_file = next((tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent").glob("*.jsonl"))
    assert actual == 0
    assert output == f"MODEL_HINT_OK\nContinue: js --model hint-model --session {session_file.stem}\n"


def test_resumed_prompt_uses_js_model_over_me_model_and_config(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setenv("JS_MODEL", "from-js-model")
    config_dir = tmp_path / ".config" / "js"
    config_dir.mkdir(parents=True)
    (config_dir / "jsrc").write_text("set model.id from-config\n", encoding="utf-8")
    session_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "resume-env-model.jsonl"
    cli.M.append_message(session_file, {"role": "user", "content": "old"})
    seen: list[str | None] = []

    def completion_stub(**kwargs):
        seen.append(kwargs.get("model_id"))
        return _fake_stream_result("ENV_MODEL_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["--session", "resume-env-model", "-p", "continue"])

    output = capsys.readouterr().out
    assert actual == 0
    assert seen == ["from-js-model"]
    assert output == "ENV_MODEL_OK\nContinue: js --session resume-env-model\n"


def test_js_prompt_mode_generated_session_prints_usable_continue_hint(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    def completion_stub(**kwargs):
        return _fake_stream_result("GENERATED_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["-p", "Reply with GENERATED_OK"])

    output = capsys.readouterr().out
    # New layout: sessions live directly under the per-agent dir.
    agent_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    session_files = list(agent_dir.glob("*.jsonl"))
    assert actual == 0
    assert len(session_files) == 1
    session_file = session_files[0]
    assert output == f"GENERATED_OK\nContinue: js --session {session_file.stem}\n"
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

    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["--no-save", "-p", "Reply with NO_SAVE_OK"])

    output = capsys.readouterr().out
    agent_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    assert actual == 0
    assert output == "NO_SAVE_OK\n"
    assert not (agent_dir / "latest.json").exists()
    assert not list(agent_dir.glob("*.jsonl"))
    assert not (agent_dir / ".no-save.jsonl").exists()


def test_js_pipe_modes_no_save_write_no_session_or_latest(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    def completion_stub(**kwargs):
        return _fake_stream_result("PIPE_NO_SAVE_OK")

    class StdinStub:
        def __init__(self, text: str):
            self.text = text

        def isatty(self):
            return False

        def read(self):
            return self.text

    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    monkeypatch.setattr(cli.sys, "stdin", StdinStub("Reply with PIPE_NO_SAVE_OK"))
    actual_pipe = cli.main(["--no-save"])
    output_pipe = capsys.readouterr().out

    monkeypatch.setattr(cli.sys, "stdin", StdinStub("Reply with PIPE_NO_SAVE_OK"))
    actual_prompt_pipe = cli.main(["--no-save", "-p"])
    output_prompt_pipe = capsys.readouterr().out

    agent_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    assert actual_pipe == 0
    assert actual_prompt_pipe == 0
    assert output_pipe == "PIPE_NO_SAVE_OK\n"
    assert output_prompt_pipe == "PIPE_NO_SAVE_OK\n"
    assert not (agent_dir / "latest.json").exists()
    assert not list(agent_dir.glob("*.jsonl"))
    assert not (agent_dir / ".no-save.jsonl").exists()

def test_short_no_save_prompt_alias_suppresses_persistence(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    def completion_stub(**kwargs):
        return _fake_stream_result("SHORT_NO_SAVE_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["-n", "-p", "Reply with SHORT_NO_SAVE_OK"])

    output = capsys.readouterr().out
    agent_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    assert actual == 0
    assert output == "SHORT_NO_SAVE_OK\n"
    assert not (agent_dir / "latest.json").exists()
    assert not list(agent_dir.glob("*.jsonl"))
    assert not (agent_dir / ".no-save.jsonl").exists()


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
        monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 120_000, raising=False)
        messages.append({"role": "assistant", "content": "PROMPT_COMPACT_OK"})

    def compact_stub(cfg, system, messages, *, forced=False, **kwargs):
        seen.append(cfg.model)
        return "compacted"

    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.runtime, "compact_messages", compact_stub)
    monkeypatch.setattr(cli.runtime, "_resolve_context_window", lambda _model, _provider: 150_000)

    actual = cli.main(["--model", "flag-model", "-p", "hi"])

    assert actual == 0
    assert "PROMPT_COMPACT_OK" in capsys.readouterr().out
    assert seen == ["flag-model"]


def test_cli_refresh_model_catalog_flag_exits_after_forced_refresh(monkeypatch, capsys):
    seen: list[str] = []

    def refresh_stub() -> bool:
        seen.append("forced")
        print("refreshed")
        return True

    monkeypatch.setattr(cli, "_force_refresh_model_catalog", refresh_stub)

    actual = cli.main(["--refresh-model-catalog"])

    assert actual == 0
    assert seen == ["forced"]
    assert "refreshed" in capsys.readouterr().out


def test_prompt_mode_reasoning_off_and_maxout_forward_explicit_overrides(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JS_REASONING", "high")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    cfg = Config(
        agent_id="test-agent",
        agent_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent",
        model="offline-test-model",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort="high",
        max_output_tokens=99,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / ".history",
        sessions_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent",
        session_file=tmp_path / ".local" / "share" / "js" / "sessions" / "test-agent" / "knobs.jsonl",
        prompts_dir=prompts,
    )
    seen: dict[str, object] = {}

    def completion_stub(**kwargs):
        seen["reasoning_effort_present"] = "reasoning_effort" in kwargs
        seen["max_output_tokens"] = kwargs["max_output_tokens"]
        return _fake_stream_result("KNOBS_OK")

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["-r", "off", "--max-out", "321", "-p", "hi"])

    output = capsys.readouterr().out
    assert actual == 0
    assert output == "KNOBS_OK\nContinue: js --session knobs\n"
    assert seen == {"reasoning_effort_present": True, "max_output_tokens": 321}


def test_wiki_and_artifact_modes_forward_debug_file_reasoning_and_maxout(monkeypatch, tmp_path):
    calls: list[dict] = []

    def run_prompt_stub(
        prompt,
        model=None,
        debug=False,
        debug_file=None,
        agent=None,
        session=None,
        save=True,
        system_override=None,
        resume_prefix=None,
        reasoning=None,
        maxout=None,
        show_continue=True,
        tool_registry=None,
        extras=None,
    ):
        calls.append(
            {
                "prompt": prompt,
                "model": model,
                "debug": debug,
                "debug_file": debug_file,
                "agent": agent,
                "session": session,
                "save": save,
                "reasoning": reasoning,
                "maxout": maxout,
                "show_continue": show_continue,
                "has_system": bool(system_override),
                "has_registry": tool_registry is not None,
                "resume_prefix": resume_prefix,
            }
        )
        return 0

    monkeypatch.setattr(cli, "_run_prompt", run_prompt_stub)
    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None, **_kwargs: Config(
        agent_id="mode-agent",
        agent_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "mode-agent",
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
        sessions_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "mode-agent",
        session_file=tmp_path / ".local" / "share" / "js" / "sessions" / "mode-agent" / "mode.jsonl",
        prompts_dir=tmp_path / "prompts",
    ))

    vault = tmp_path / "wiki-forward"
    vault.mkdir()
    wiki_rc = cli.main(["--wiki=ingest", "--vault", str(vault), "--debug-file", str(tmp_path / "wiki.log"), "-r", "off", "--max-out", "111", "-m", "model-a", "-n", str(tmp_path)])
    artifact_rc = cli.main(["--artifact=query", "--debug-file", str(tmp_path / "artifact.log"), "-r", "low", "--max-out", "222", "-m", "model-b", "-n", "find thing"])

    assert wiki_rc == 0
    assert artifact_rc == 0
    assert calls[0]["debug_file"] == str(tmp_path / "wiki.log")
    assert calls[0]["reasoning"] == "off"
    assert calls[0]["maxout"] == 111
    assert calls[0]["model"] == "model-a"
    assert calls[0]["has_system"] is True
    assert calls[0]["has_registry"] is True
    assert calls[1]["debug_file"] == str(tmp_path / "artifact.log")
    assert calls[1]["reasoning"] == "low"
    assert calls[1]["maxout"] == 222
    assert calls[1]["model"] == "model-b"
    assert calls[1]["has_system"] is True
    assert calls[1]["has_registry"] is True


def test_offline_compact_model_flag_overrides_same_model(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "compact-session.jsonl"
    cli.M.append_message(session_file, {"role": "user", "content": "old"})
    seen: list[str] = []

    def compact_stub(cfg, system, messages, *, focus="", forced=False):
        seen.append(cfg.model)
        return "compacted"

    monkeypatch.setattr(cli.runtime, "compact_messages", compact_stub)

    actual = cli.main(["--compact", "compact-session", "--model", "compact-model"])

    assert actual == 0
    assert "compacted" in capsys.readouterr().out
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
    assert "SURVEY" in prompt
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


def test_commit_mode_rejects_agent_override_and_missing_target(monkeypatch, tmp_path, capsys):
    missing = tmp_path / "missing"

    with_agent = cli.main(["--commit", "--agent", "autocoder"])
    missing_target = cli.main(["--commit", str(missing)])

    captured = capsys.readouterr()
    assert with_agent == 2
    assert missing_target == 2
    assert "built-in commit agent" in captured.err
    assert "commit target does not exist" in captured.err


def test_resumed_prompt_model_override_is_used_and_preserved_in_continue_hint(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    session_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "resume-model.jsonl"
    cli.M.append_message(session_file, {"role": "user", "content": "old"})
    seen: list[str | None] = []

    def run_turn_stub(cfg, system, messages, telemetry, **kwargs):
        seen.append(kwargs.get("model_override"))
        messages.append({"role": "assistant", "content": "RESUME_MODEL_OK"})

    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)

    actual = cli.main(["--session", "resume-model", "--model", "resume-model-override", "-p", "continue"])

    output = capsys.readouterr().out
    assert actual == 0
    assert seen == ["resume-model-override"]
    assert output == "RESUME_MODEL_OK\nContinue: js --model resume-model-override --session resume-model\n"


def test_short_session_alias_loads_existing_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    # New layout: sessions live directly under the per-agent dir.
    agent_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    sessions_dir = agent_dir
    sessions_dir.mkdir(parents=True)
    session_file = sessions_dir / "short-session.jsonl"
    cli.M.append_message(session_file, {"role": "user", "content": "old"})

    def completion_stub(**kwargs):
        return _fake_stream_result("SHORT_SESSION_OK")

    monkeypatch.setattr(cli.P, "load_prompt", lambda prompts_dir: "SYSTEM\n")
    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["-s", session_file.stem, "-p", "Reply with SHORT_SESSION_OK"])

    output = capsys.readouterr().out
    assert actual == 0
    assert output == "SHORT_SESSION_OK\nContinue: js --session short-session\n"
    assert load_messages(session_file) == [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "Reply with SHORT_SESSION_OK"},
        {"role": "assistant", "content": "SHORT_SESSION_OK"},
    ]


def test_short_agent_alias_scopes_session_lookup(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    # Sessions live directly under the platform data sessions/<agent>/ dir.
    kingape_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "kingape"
    default_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    kingape_sessions_dir = kingape_dir
    default_sessions_dir = default_dir
    kingape_sessions_dir.mkdir(parents=True)
    default_sessions_dir.mkdir(parents=True)
    session_name = "scoped-session.jsonl"
    kingape_session = kingape_sessions_dir / session_name
    default_session = default_sessions_dir / session_name
    cli.M.append_message(kingape_session, {"role": "user", "content": "kingape old"})
    cli.M.append_message(default_session, {"role": "user", "content": "default old"})
    loaded_prompt_dirs = []

    def completion_stub(**kwargs):
        return _fake_stream_result("KINGAPE_SESSION_OK")

    def load_prompt_spec_stub(prompts_dir):
        loaded_prompt_dirs.append(prompts_dir)
        return cli.P.PromptSpec(system="SYSTEM\n", tool_selectors=())

    monkeypatch.setattr(cli.P, "load_prompt_spec", load_prompt_spec_stub)
    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["-a", "kingape", "-s", "scoped-session", "-p", "Reply with KINGAPE_SESSION_OK"])

    output = capsys.readouterr().out
    assert actual == 0
    assert output == "KINGAPE_SESSION_OK\nContinue: js --session scoped-session\n"
    assert loaded_prompt_dirs[0].name == "kingape"
    assert load_messages(kingape_session) == [
        {"role": "user", "content": "kingape old"},
        {"role": "user", "content": "Reply with KINGAPE_SESSION_OK"},
        {"role": "assistant", "content": "KINGAPE_SESSION_OK"},
    ]
    assert load_messages(default_session) == [{"role": "user", "content": "default old"}]


def test_wiki_continue_hint_preserves_model_override(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    def completion_stub(**kwargs):
        return _fake_stream_result("WIKI_MODEL_HINT_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    vault = tmp_path / "wiki-hints"
    vault.mkdir()
    actual = cli.main(["--wiki=ingest", "--vault", str(vault), "--model", "wiki-model"])

    output = capsys.readouterr().out
    session_file = next((tmp_path / ".local" / "share" / "js" / "sessions" / "wiki").glob("*.jsonl"))
    assert actual == 0
    assert output == (
        "WIKI_MODEL_HINT_OK\n"
        f"Continue: js --wiki=ingest --vault={vault} --model wiki-model --session {session_file.stem}\n"
    )


def test_wiki_combined_modes_run_as_sequential_turns_in_one_session(monkeypatch, tmp_path):
    # Regression: ingest's prompt says "then stop" and a text-only turn ends the
    # loop, so cramming ingest+synthesize into one prompt stranded the run at the
    # seam. Each mode must run as its own driven turn over one shared session.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    systems: list[str] = []

    def completion_stub(**kwargs):
        systems.append(kwargs["messages"][0].parts[0].text)
        return _fake_stream_result(f"turn{len(systems)} done.")

    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["--wiki=ingest,synthesize", "--vault", str(tmp_path)])

    assert actual == 0
    assert len(systems) == 2
    assert "## MODE: INGEST" in systems[0] and "## MODE: SYNTHESIZE" not in systems[0]
    assert "## MODE: SYNTHESIZE" in systems[1] and "## MODE: INGEST" not in systems[1]

    sessions_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "wiki"
    nonempty = [p for p in sessions_dir.glob("*.jsonl") if p.stat().st_size > 0]
    assert len(nonempty) == 1
    roles = [m["role"] for m in load_messages(nonempty[0])]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_artifact_combined_modes_run_as_sequential_turns_in_one_session(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)

    systems: list[str] = []

    def completion_stub(**kwargs):
        systems.append(kwargs["messages"][0].parts[0].text)
        return _fake_stream_result(f"artifact turn {len(systems)} done.")

    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

    actual = cli.main(["--artifact=curate,digest"])

    assert actual == 0
    assert len(systems) == 2
    assert "## MODE: CURATE" in systems[0] and "## MODE: DIGEST" not in systems[0]
    assert "## MODE: DIGEST" in systems[1] and "## MODE: CURATE" not in systems[1]

    sessions_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "artifact"
    nonempty = [p for p in sessions_dir.glob("*.jsonl") if p.stat().st_size > 0]
    assert len(nonempty) == 1
    roles = [m["role"] for m in load_messages(nonempty[0])]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_artifact_invalid_mode_is_rejected(capsys):
    actual = cli.main(["--artifact=banana"])

    captured = capsys.readouterr()
    assert actual == 2
    assert "--artifact" in captured.err
    assert "curate" in captured.err


def test_artifact_tools_are_registered():
    names = set(build_default_registry().by_name)

    assert {
        "artifact_overview",
        "artifact_search",
        "artifact_read",
        "artifact_curate",
        "artifact_write_page",
        "artifact_ingest",
    }.issubset(names)


def _auto_compact_cfg(tmp_path, *, compact: dict | None = None) -> Config:
    return Config(
        agent_id="auto",
        agent_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "auto",
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
        sessions_dir=tmp_path / ".local" / "share" / "js" / "sessions" / "auto",
        session_file=tmp_path / ".local" / "share" / "js" / "sessions" / "auto" / "auto.jsonl",
        prompts_dir=tmp_path / "prompts",
        settings={"compact": {"context_window": 100, **(compact or {})}},
    )


def _auto_state() -> dict:
    return {
        "system": "SYSTEM",
        "messages": [{"role": "user", "content": "hi"}],
        "compact_notified": False,
        "compact_consecutive": 0,
        "compact_paused": False,
    }


def test_auto_compact_noops_when_disabled_or_paused(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 95, raising=False)
    monkeypatch.setattr(cli.runtime, "compact_messages", lambda *a, **kw: calls.append(kw) or "compacted")

    disabled = _auto_compact_cfg(tmp_path, compact={"auto": False})
    cli._maybe_auto_compact(disabled, _auto_state())
    paused_state = _auto_state()
    paused_state["compact_paused"] = True
    cli._maybe_auto_compact(_auto_compact_cfg(tmp_path), paused_state)

    assert calls == []
    assert capsys.readouterr().out == ""


def test_auto_compact_notifies_once_at_threshold_and_resets_below(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.runtime, "compact_messages", lambda *a, **kw: calls.append(kw) or "compacted")
    cfg = _auto_compact_cfg(tmp_path)
    state = _auto_state()

    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 49, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 50, raising=False)
    cli._maybe_auto_compact(cfg, state)
    first = capsys.readouterr().out
    cli._maybe_auto_compact(cfg, state)
    second = capsys.readouterr().out
    assert "50% full" in first
    assert second == ""
    assert calls == []

    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 40, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert state["compact_notified"] is False
    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 50, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert "50% full" in capsys.readouterr().out


def test_auto_compact_uses_active_model_for_same(monkeypatch, tmp_path):
    seen: list[str] = []

    def compact_stub(cfg, system, messages, *, forced=False, **kwargs):
        seen.append(cfg.model)
        return "compacted"

    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 80, raising=False)
    monkeypatch.setattr(cli.runtime, "compact_messages", compact_stub)
    cfg = _auto_compact_cfg(tmp_path)
    state = _auto_state()
    state["model"] = "active-model"

    cli._maybe_auto_compact(cfg, state)

    assert seen == ["active-model"]


def test_auto_compact_triggers_at_80_and_forces_at_90(monkeypatch, tmp_path):
    calls: list[dict] = []

    def compact_stub(cfg, system, messages, *, forced=False, **kwargs):
        calls.append({"forced": forced, "system": system, "messages": messages})
        return "compacted"

    monkeypatch.setattr(cli.runtime, "compact_messages", compact_stub)
    cfg = _auto_compact_cfg(tmp_path)

    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 80, raising=False)
    cli._maybe_auto_compact(cfg, _auto_state())
    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 90, raising=False)
    cli._maybe_auto_compact(cfg, _auto_state())

    assert [call["forced"] for call in calls] == [False, True]


def test_auto_compact_pauses_after_two_consecutive_fires_and_resets_below_trigger(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.runtime, "compact_messages", lambda *a, **kw: calls.append(kw) or "compacted")
    cfg = _auto_compact_cfg(tmp_path)
    state = _auto_state()

    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 80, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert state["compact_consecutive"] == 1
    assert state["compact_paused"] is False
    cli._maybe_auto_compact(cfg, state)
    assert state["compact_consecutive"] == 2
    assert state["compact_paused"] is True
    assert "auto-compaction paused" in capsys.readouterr().out
    cli._maybe_auto_compact(cfg, state)
    assert len(calls) == 2

    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 79, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert state["compact_consecutive"] == 0
    assert state["compact_paused"] is False
    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 80, raising=False)
    cli._maybe_auto_compact(cfg, state)
    assert len(calls) == 3


def test_auto_compact_invalid_numeric_config_falls_back_to_defaults(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.runtime, "compact_messages", lambda *a, **kw: calls.append(kw) or "compacted")

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
        monkeypatch.setattr(cli.runtime, "_resolve_context_window", lambda _model, _provider: 131072)
        monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 104858, raising=False)  # ~80% of mocked metadata window
        cli._maybe_auto_compact(cfg, _auto_state())

    assert len(calls) == 2
    assert [call["forced"] for call in calls] == [False, False]
    assert capsys.readouterr().out.count("80% full") == 2


def test_auto_compact_misordered_thresholds_use_safe_defaults(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.runtime, "compact_messages", lambda *a, **kw: calls.append(kw) or "compacted")
    cfg = _auto_compact_cfg(
        tmp_path,
        compact={
            "notify_threshold": 0.95,  # invalid: notify after trigger
            "trigger_threshold": 0.80,
            "force_threshold": 0.70,   # invalid: force before trigger
        },
    )

    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 80, raising=False)
    cli._maybe_auto_compact(cfg, _auto_state())

    assert len(calls) == 1
    assert calls[0]["forced"] is False
    assert "80% full" in capsys.readouterr().out


def test_auto_compact_string_false_values_disable_auto(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(cli.runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 95, raising=False)
    monkeypatch.setattr(cli.runtime, "compact_messages", lambda *a, **kw: calls.append(kw) or "compacted")

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
        seen["ctx_cwd"] = str(runtime.T.DEFAULT_CONTEXT.cwd)
        return _fake_stream_result("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model", completion_stub)

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


def test_dash_C_rejects_missing_dir(tmp_path, capsys):
    missing = tmp_path / "nope"
    actual = cli.main(["-C", str(missing), "-p", "hi"])
    assert actual == 2
    assert "not a directory" in capsys.readouterr().err
