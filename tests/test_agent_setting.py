"""The `agent` setting picks the agent a run uses when no flag names one.

Precedence: --agent/--commit beats env JS_AGENT, which beats the jsrc layers,
which beat js/jsrc. The paths that need the agent before the Config exists
(--last, --session-key) resolve it the same way.
"""

from __future__ import annotations

from pathlib import Path

import ai.types.usage
import pytest

from js import cli, runtime, settings
from js.config import derive_session_name, from_env
from js.memory import load_messages
from js.model_client import ModelStreamResult
from repl_driver import LineSession

AGENTS = ("fromjsrc", "fromenv", "fromflag")

# (JS_AGENT value or None, extra argv, the agent that must run)
LAYERS = [
    pytest.param(None, [], "fromjsrc", id="jsrc"),
    pytest.param("fromenv", [], "fromenv", id="env-over-jsrc"),
    pytest.param("fromenv", ["--agent", "fromflag"], "fromflag", id="flag-over-env-and-jsrc"),
]


@pytest.fixture(autouse=True)
def fresh_tool_context(monkeypatch, tmp_path):
    from js.toolkit.core import ToolContext

    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))


def _home(monkeypatch, tmp_path: Path, js_agent: str | None) -> Path:
    """A tmp HOME whose global jsrc says `set agent fromjsrc`, with one personal
    agent per layer. Returns the project directory, which is also the cwd."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for spec in settings.REGISTRY:
        for name in settings.env_names_for(spec):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    if js_agent is not None:
        monkeypatch.setenv("JS_AGENT", js_agent)
    for agent in AGENTS:
        agent_dir = home / ".js" / "agents" / agent
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text("tools: []\n", encoding="utf-8")
        (agent_dir / "01-prompt.md").write_text(f"{agent}\n", encoding="utf-8")
    (home / ".js" / "jsrc").write_text("set agent fromjsrc\n", encoding="utf-8")
    monkeypatch.chdir(project)
    return project


def _agent_flag(argv: list[str]) -> str | None:
    return argv[argv.index("--agent") + 1] if "--agent" in argv else None


def _sessions(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "home" / ".js" / "sessions").rglob("*.jsonl"))


def _fake_stream_result(text: str) -> ModelStreamResult:
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )


def test_package_default_agent_is_defaultagent():
    assert settings.default_value("agent") == "defaultagent"


@pytest.mark.parametrize(("js_agent", "argv", "expected"), LAYERS)
def test_config_resolves_agent_by_layer(monkeypatch, tmp_path, js_agent, argv, expected):
    _home(monkeypatch, tmp_path, js_agent)

    cfg = from_env(save_session=False, agent_id=_agent_flag(argv))

    assert cfg.agent_id == expected
    assert cfg.prompts_dir == tmp_path / "home" / ".js" / "agents" / expected


def test_extra_agent_beats_env(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path, "fromenv")

    cfg = from_env(save_session=False, extras=["agent=fromflag"])

    assert cfg.agent_id == "fromflag"


def test_unsafe_agent_in_jsrc_is_rejected(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path, None)
    (tmp_path / "home" / ".js" / "jsrc").write_text("set agent ../../etc\n", encoding="utf-8")

    with pytest.raises(ValueError):
        from_env(save_session=False)


@pytest.mark.parametrize(("js_agent", "argv", "expected"), LAYERS)
def test_last_resumes_the_session_of_the_resolved_agent(monkeypatch, tmp_path, js_agent, argv, expected):
    _home(monkeypatch, tmp_path, js_agent)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.runtime, "run_turn", lambda *a, **k: None)
    monkeypatch.setattr(cli, "PromptSession", lambda *a, **k: LineSession(["first run"]))
    assert cli.main(["--blocking", *argv]) == 0
    [first] = _sessions(tmp_path)
    assert first.parent.name == expected
    resumed: list = []

    def record(cfg, system, messages, *a, **k):
        resumed.append((cfg.agent_id, [m["content"] for m in messages]))

    monkeypatch.setattr(cli, "PromptSession", lambda *a, **k: LineSession(["second run"]))
    monkeypatch.setattr(cli.runtime, "run_turn", record)

    assert cli.main(["--blocking", "--last", *argv]) == 0

    assert _sessions(tmp_path) == [first]
    assert resumed == [(expected, ["first run", "second run"])]


@pytest.mark.parametrize(("js_agent", "argv", "expected"), LAYERS)
def test_session_key_derives_from_the_resolved_agent(monkeypatch, tmp_path, js_agent, argv, expected):
    project = _home(monkeypatch, tmp_path, js_agent)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **_kwargs: _fake_stream_result("OK"))

    assert cli.main(["--session-key", "job-7", *argv, "-p", "hello"]) == 0

    [session] = _sessions(tmp_path)
    expected_file = (
        tmp_path / "home" / ".js" / "sessions" / expected / f"{derive_session_name(expected, project, 'job-7')}.jsonl"
    )
    assert session == expected_file
    assert [m["content"] for m in load_messages(session)] == ["hello", "OK"]
