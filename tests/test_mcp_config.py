from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from js import mcp_config, setcmd, settings
from js.config import from_env

ENV_SECRET = "MCP_ENV_SENTINEL_DO_NOT_PRINT"
HEADER_SECRET = "MCP_HEADER_SENTINEL_DO_NOT_PRINT"


def _servers(**updates):
    value = {
        "Local Files": {
            "command": "mcp-files",
            "args": ["--safe"],
            "env": {"TOKEN": ENV_SECRET},
        },
        "remote": {
            "url": "https://mcp.example.test/rpc",
            "headers": {"Authorization": f"Bearer {HEADER_SECRET}"},
        },
        "disabled": {"enabled": False, "command": "never-start"},
    }
    value.update(updates)
    return value


def _agents():
    return {
        "writer": {
            "servers": {"allow": ["*"], "deny": ["remote"]},
            "tools": {"allow": ["*__read*"], "deny": ["*__read_secret"]},
        }
    }


def test_resolve_both_transports_defaults_enabled_filtering_and_immutability():
    resolved = mcp_config.resolve(
        {"mcp": {"servers": _servers(), "agents": {}}},
        "defaultagent",
    )

    assert [server.name for server in resolved.servers] == ["Local Files", "remote"]
    local, remote = resolved.servers
    assert local.normalized_name == "local_files"
    assert local.transport == "stdio"
    assert local.command == "mcp-files"
    assert local.args == ("--safe",)
    assert local.env["TOKEN"] == ENV_SECRET
    assert remote.transport == "streamable-http"
    assert remote.url == "https://mcp.example.test/rpc"
    assert remote.headers["Authorization"].endswith(HEADER_SECRET)
    assert resolved.policy.allows_server("anything") is True
    assert resolved.allows_tool("local_files__anything") is True
    with pytest.raises(FrozenInstanceError):
        local.command = "changed"
    with pytest.raises(TypeError):
        local.env["TOKEN"] = "changed"


def test_active_agent_server_and_tool_allow_deny_rules_have_deny_precedence():
    resolved = mcp_config.resolve(
        {"mcp": {"servers": _servers(), "agents": _agents()}},
        "writer",
    )

    assert [server.name for server in resolved.servers] == ["Local Files"]
    assert resolved.allows_tool("local_files__read_file") is True
    assert resolved.allows_tool("local_files__write_file") is False
    assert resolved.allows_tool("local_files__read_secret") is False


def test_jsrc_env_extra_layering_reaches_active_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    project = tmp_path / "project"
    project_js = project / ".js"
    project_js.mkdir(parents=True)
    file_servers = {"file": {"command": "from-file"}}
    env_servers = {"env": {"command": "from-env"}}
    extra_servers = {"extra": {"url": "http://localhost:7777/mcp"}}
    (project_js / "jsrc").write_text(
        f"set mcp.servers {json.dumps(file_servers)}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JS_MCP_SERVERS", json.dumps(env_servers))

    env_config = from_env(cwd=project, save_session=False, ignore_global_config=True)
    cli_config = from_env(
        cwd=project,
        save_session=False,
        ignore_global_config=True,
        extras=[f"mcp.servers={json.dumps(extra_servers)}"],
    )

    assert [server.name for server in env_config.mcp.servers] == ["env"]
    assert [server.name for server in cli_config.mcp.servers] == ["extra"]


def test_mcp_server_secret_is_masked_in_mutation_show_and_transcript_lines():
    live = settings.seed_defaults()
    raw = json.dumps(_servers())

    changed = setcmd.run_repl_command(live, f"/set mcp.servers {raw}")
    shown = setcmd.run_repl_command(live, "/show mcp.servers")
    transcript = "\n".join([*changed.lines, *shown.lines])

    assert changed.lines == ["mcp.servers = <set>"]
    assert shown.lines[0] == "mcp.servers = <set>"
    assert ENV_SECRET not in transcript
    assert HEADER_SECRET not in transcript


def test_save_round_trips_actual_secret_server_configuration(tmp_path):
    live = settings.seed_defaults()
    raw = json.dumps(_servers())
    result = setcmd.run_repl_command(live, f"/set mcp.servers {raw}")
    assert result.error is None
    path = tmp_path / "jsrc"

    count, backup = settings.save_settings_to_jsrc(path, live, stamp="test")
    saved = path.read_text(encoding="utf-8")
    reloaded = settings.collect_settings(config_paths=[path], env={})

    assert count == 1
    assert backup is None
    assert ENV_SECRET in saved and HEADER_SECRET in saved
    assert reloaded["mcp"]["servers"] == _servers()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"x": {}}, "exactly one of command or url"),
        ({"x": {"command": "cmd", "url": "https://example.test"}}, "exactly one"),
        ({"x": {"command": 3}}, "command must be a non-empty string"),
        ({"x": {"command": "cmd", "args": "no"}}, "args must be a list"),
        ({"x": {"command": "cmd", "env": {"TOKEN": 3}}}, "env must be an object"),
        ({"x": {"url": "ftp://example.test"}}, "must use http or https"),
        ({"x": {"url": "https://user:pass@example.test"}}, "must not contain userinfo"),
        ({"x": {"url": "https://example.test", "headers": {"X": 3}}}, "headers must be"),
        ({"x": {"enabled": "yes", "command": "cmd"}}, "enabled must be a boolean"),
        ({"A B": {"command": "one"}, "a_b": {"command": "two"}}, "normalize to the same"),
    ],
)
def test_server_validation_failures_do_not_mutate(value, message):
    live = settings.seed_defaults()
    result = setcmd.run_repl_command(live, f"/set mcp.servers {json.dumps(value)}")

    assert result.changed is False
    assert message in result.error
    assert live["mcp"]["servers"] == {}


def test_duplicate_json_server_names_are_rejected():
    live = settings.seed_defaults()
    result = setcmd.run_repl_command(
        live,
        '/set mcp.servers {"same":{"command":"one"},"same":{"command":"two"}}',
    )
    assert result.changed is False
    assert "duplicate object key" in result.error


@pytest.mark.parametrize(
    "value",
    [
        {"agent": []},
        {"agent": {"servers": []}},
        {"agent": {"servers": {"allow": "*"}}},
        {"agent": {"tools": {"deny": [3]}}},
        {"agent": {"unknown": {}}},
    ],
)
def test_agent_policy_validation_failures(value):
    live = settings.seed_defaults()
    result = setcmd.run_repl_command(live, f"/set mcp.agents {json.dumps(value)}")
    assert result.changed is False
    assert result.error is not None
    assert live["mcp"]["agents"] == {}
