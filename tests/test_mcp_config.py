from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from js import messages as msgs
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
        {"MCP_SERVERS": _servers(), "MCP_AGENT_POLICY": {}},
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
        {"MCP_SERVERS": _servers(), "MCP_AGENT_POLICY": _agents()},
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
        f"set MCP_SERVERS {json.dumps(file_servers)}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JS_MCP_SERVERS", json.dumps(env_servers))

    env_config = from_env(cwd=project, save_session=False, ignore_global_config=True)
    cli_config = from_env(
        cwd=project,
        save_session=False,
        ignore_global_config=True,
        extras=[f"MCP_SERVERS={json.dumps(extra_servers)}"],
    )

    assert [server.name for server in env_config.mcp.servers] == ["env"]
    assert [server.name for server in cli_config.mcp.servers] == ["extra"]


def test_mcp_server_secret_is_masked_in_mutation_show_and_transcript_lines():
    live = settings.seed_defaults()
    setcmd.set_command(live, "PARANOID_MODE on")
    raw = json.dumps(_servers())

    changed = setcmd.set_command(live, f"MCP_SERVERS {raw}")
    shown = setcmd.show_lines(live, "MCP_SERVERS")
    transcript = "\n".join([*changed.lines, *shown.lines])

    assert changed.lines == [msgs.SETTING_CHANGED.line(colour=False, key="MCP_SERVERS", value="<set>")]
    assert shown.lines[0] == msgs.SETTING_IS.line(colour=False, key="MCP_SERVERS", value="<set>")
    assert ENV_SECRET not in transcript
    assert HEADER_SECRET not in transcript


def test_save_round_trips_actual_secret_server_configuration(tmp_path):
    live = settings.seed_defaults()
    raw = json.dumps(_servers())
    result = setcmd.set_command(live, f"MCP_SERVERS {raw}")
    assert result.error is None
    path = tmp_path / "jsrc"

    settings.save_settings_to_jsrc(path, live, keys=["MCP_SERVERS"])
    saved = path.read_text(encoding="utf-8")
    reloaded = settings.collect_settings(config_paths=[path], env={})

    assert ENV_SECRET in saved and HEADER_SECRET in saved
    assert reloaded["MCP_SERVERS"] == _servers()


@pytest.mark.parametrize(
    "value",
    [
        {"x": {}},
        {"x": {"command": "cmd", "url": "https://example.test"}},
        {"x": {"command": 3}},
        {"x": {"command": "cmd", "args": "no"}},
        {"x": {"command": "cmd", "env": {"TOKEN": 3}}},
        {"x": {"url": "ftp://example.test"}},
        {"x": {"url": "https://user:pass@example.test"}},
        {"x": {"url": "https://example.test", "headers": {"X": 3}}},
        {"x": {"enabled": "yes", "command": "cmd"}},
        {"A B": {"command": "one"}, "a_b": {"command": "two"}},
    ],
)
def test_server_validation_failures_do_not_mutate(value):
    live = settings.seed_defaults()
    result = setcmd.set_command(live, f"MCP_SERVERS {json.dumps(value)}")

    assert result.changed is False
    assert result.error
    assert live["MCP_SERVERS"] == {}


def test_duplicate_json_server_names_are_rejected():
    live = settings.seed_defaults()
    result = setcmd.set_command(
        live,
        'MCP_SERVERS {"same":{"command":"one"},"same":{"command":"two"}}',
    )
    assert result.changed is False
    assert result.error


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
    result = setcmd.set_command(live, f"MCP_AGENT_POLICY {json.dumps(value)}")
    assert result.changed is False
    assert result.error is not None
    assert live["MCP_AGENT_POLICY"] == {}


def test_deny_on_the_normalized_name_also_disables_the_server():
    """`Local Files` goes by `local_files` everywhere; a deny on either name wins."""
    for deny in (["local_files"], ["Local Files"], ["local_*"]):
        resolved = mcp_config.resolve(
            {"MCP_SERVERS": _servers(), "MCP_AGENT_POLICY": {"a": {"servers": {"deny": deny}}}}, "a")
        assert "Local Files" not in [server.name for server in resolved.servers], deny
        assert resolved.policy.allows_server("Local Files", "local_files") is False
    allowed = mcp_config.resolve(
        {"MCP_SERVERS": _servers(), "MCP_AGENT_POLICY": {"a": {"servers": {"allow": ["local_files"]}}}}, "a")
    assert [server.name for server in allowed.servers] == ["Local Files"]


@pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf"), "30", True])
def test_bad_request_timeout_is_a_config_error(bad):
    with pytest.raises(mcp_config.MCPConfigError):
        mcp_config.resolve({"MCP_SERVERS": {}, "MCP_AGENT_POLICY": {}, "MCP_TIMEOUT": bad}, "a")


def test_request_timeout_values_that_resolve():
    for raw, want in ((None, None), (30, 30.0), (0.5, 0.5)):
        assert mcp_config.resolve({"MCP_TIMEOUT": raw}, "a").request_timeout_s == want
