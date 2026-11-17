"""JSON listing options and provider credential masking."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from js import cli, listings, logins, paths, providers


@pytest.fixture
def tmp_logins_dir(tmp_path: Path):
    """Isolate the login/cache store, like tests/test_logins.py."""
    logins.set_config_dir(tmp_path)
    yield tmp_path
    logins.set_config_dir(paths.login_store_dir())


def _stdout_json(capsys) -> object:
    out = capsys.readouterr().out
    # listings.print_json writes exactly one json.dumps line.
    assert out.endswith("\n")
    return json.loads(out)


# --------------------------------------------------------------------------
# --list-providers --json
# --------------------------------------------------------------------------

def test_providers_json_shape_and_sources(monkeypatch, tmp_logins_dir):
    # Make deepseek look env-configured; save a login for a different provider.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-deepseek")
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    logins.save_login(logins.Login(provider_id="mimo", provider_api_key="sk-mimo"))

    rows = listings.providers_json()

    assert isinstance(rows, list)
    # Every row is exactly {id, name, source}.
    for row in rows:
        assert set(row) == {"id", "name", "source"}
        assert isinstance(row["id"], str)
        assert isinstance(row["name"], str)
        assert row["source"] in {"login", "env", "registry", "custom"}

    by_id = {row["id"]: row for row in rows}
    # Builtin login providers are present.
    assert "deepseek" in by_id
    assert "mimo" in by_id
    # source precedence: login wins over env wins over registry.
    assert by_id["mimo"]["source"] == "login"
    assert by_id["deepseek"]["source"] == "env"


def test_providers_json_custom_saved_only_provider_appended(monkeypatch, tmp_logins_dir):
    # A saved login whose id is not in the known registry shows up as "custom".
    known = {p.id for p in providers.login_providers()}
    custom_id = "my-private-proxy-xyz"
    assert custom_id not in known
    logins.save_login(
        logins.Login(provider_id=custom_id, provider_base_url="http://proxy.test/v1", provider_api_key="sk-x")
    )

    rows = listings.providers_json()

    by_id = {row["id"]: row for row in rows}
    assert by_id[custom_id] == {"id": custom_id, "name": custom_id, "source": "custom"}


# --------------------------------------------------------------------------
# --list-logins --json  (secret masking is the point of this flag)
# --------------------------------------------------------------------------

def test_logins_json_empty_store(tmp_logins_dir):
    assert listings.logins_json() == []


def test_logins_json_hides_credentials_when_paranoid_mode_is_on(tmp_logins_dir, capsys):
    logins.save_login(logins.Login(
        provider_id="private-proxy", provider_api_key="key-secret",
        provider_headers={"Authorization": "Bearer header-secret"},
    ))

    assert cli.main(["--extra", "PARANOID_MODE=on", "--list-logins", "--json"]) == 0
    rows = _stdout_json(capsys)

    assert rows[0]["provider_id"] == "private-proxy"
    assert rows[0]["has_api_key"] is True
    assert all(secret not in json.dumps(rows) for secret in (
        "key-secret", "header-secret",
    ))


def test_json_flags_print_their_payload_as_one_json_line(monkeypatch, capsys):
    monkeypatch.setattr(listings, "providers_json", lambda: [{"id": "p"}])
    monkeypatch.setattr(listings, "logins_json", lambda: [{"provider_id": "l"}])

    assert cli.main(["--list-providers", "--json"]) == 0
    assert _stdout_json(capsys) == [{"id": "p"}]
    assert cli.main(["--list-logins", "--json"]) == 0
    assert _stdout_json(capsys) == [{"provider_id": "l"}]


# --------------------------------------------------------------------------
# --list-models --json
# --------------------------------------------------------------------------

def test_models_json_shape_with_stubbed_provider(monkeypatch, tmp_logins_dir):
    # Stub the network boundary (logins.test_login) the way prompt-mode tests
    # stub stream_model, then assert the {"models": [...]} envelope.
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))
    monkeypatch.setattr(cli.logins, "test_login", lambda login: ["m-alpha", "m-beta"])

    assert listings.models_json("deepseek") == {"models": ["deepseek/m-alpha", "deepseek/m-beta"]}


def test_models_json_error_path_is_json_and_returns_one(tmp_logins_dir, capsys, monkeypatch):
    # Offline, with no saved login and no creds, the SDK raises on missing
    # credentials before any request; cli wraps it as {"error": ...}, rc=1.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)

    rc = cli.main(["--list-models", "openai", "--json"])
    payload = _stdout_json(capsys)

    assert rc == 1
    assert isinstance(payload, dict)
    assert set(payload) == {"error"}
    assert isinstance(payload["error"], str)
    assert payload["error"]


def test_models_json_no_arg_uses_config_provider(monkeypatch, tmp_logins_dir, capsys):
    # `--list-models --json` with no value (const="") sends provider_arg=None, which
    # builds a Config and feeds cfg.provider_id to logins.models_for_provider
    #. Stub the network boundary and capture the cfg.
    seen: list[str | None] = []

    def fake_test_login(login):
        seen.append(login.provider_id)
        return ["cfg-model"]

    logins.save_login(
        logins.Login(
            provider_id="that-provider",
            sdk_provider_id="openai",
            provider_base_url="http://provider.test/v1",
            provider_api_key="sk-test",
        )
    )
    monkeypatch.setenv("JS_PROVIDER", "that-provider")
    monkeypatch.setattr(cli.logins, "test_login", fake_test_login)

    rc = cli.main(["--list-models", "--json"])
    payload = _stdout_json(capsys)

    assert rc == 0
    assert payload == {"models": ["that-provider/cfg-model"]}
    assert seen == ["that-provider"]


def test_list_models_loads_the_preset_on_its_command_line(monkeypatch, tmp_logins_dir, tmp_path, capsys):
    # The provider --list-models covers comes from the settings the command
    # line loads, a preset among them.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".js").mkdir(exist_ok=True)
    (tmp_path / ".js" / "jsrc.pick").write_text(
        "set PROVIDER openai\nset API_KEY fixture\nset MODEL preset-model\n", encoding="utf-8")
    logins.cache_models("openai", ["preset-model"])

    assert cli.main(["--list-models", "--json"]) == 0
    assert _stdout_json(capsys) == {"models": []}

    assert cli.main(["--list-models", "--json", "--preset", "pick"]) == 0
    assert _stdout_json(capsys) == {"models": ["openai/preset-model"]}


def test_list_models_human_output_is_provider_slash_model(monkeypatch, tmp_logins_dir, capsys):
    logins.save_login(logins.Login(provider_id="openai", provider_api_key="sk"))
    monkeypatch.setattr(cli.logins, "test_login", lambda login: ["gpt-5.4", "gpt-5.5"])

    rc = listings.print_model_list("openai", None)
    out = capsys.readouterr().out

    assert rc == 0
    # Concise: exactly one `provider/model` per line, nothing else.
    assert out == "openai/gpt-5.4\nopenai/gpt-5.5\n"


def test_list_models_no_arg_covers_every_saved_login(monkeypatch, tmp_logins_dir, capsys):
    # A just-added provider must appear even when it is not the active one — the
    # listing spans saved logins, served from the model cache (offline-friendly).
    logins.save_login(logins.Login(provider_id="opencode-go", provider_api_key="sk"))
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk"))
    logins.cache_models("opencode-go", ["glm-5.1", "glm-5.2"])
    logins.cache_models("deepseek", ["deepseek-v4-flash"])

    def _no_live(*_a, **_k):
        raise AssertionError("cache hit should avoid a live fetch")

    monkeypatch.setattr(cli.logins, "test_login", _no_live)
    rc = listings.print_model_list(None, SimpleNamespace(provider_id=None))
    out = capsys.readouterr().out

    assert rc == 0
    assert "opencode-go/glm-5.1" in out
    assert "opencode-go/glm-5.2" in out
    assert "deepseek/deepseek-v4-flash" in out


def test_list_models_flag_prints_the_model_list(monkeypatch):
    calls: list[str | None] = []
    monkeypatch.setattr(listings, "print_model_list", lambda provider_arg, cfg: calls.append(provider_arg) or 0)

    assert cli.main(["--list-models", "openai"]) == 0
    assert calls == ["openai"]


@pytest.mark.parametrize("argv,missing", [
    (["--json", "--json-stream", "-p", "hello"], "--json-stream"),
    *[([flag], "--json") for flag in ("--list-sessions", "--list-providers", "--list-logins")],
    *[([flag, "--json-stream"], flag) for flag in ("--list-models", "--list-agents", "--list-sessions", "--list-providers", "--list-logins", "--verify")],
    *[([flag, "--json"], flag) for flag in ("--commit", "--compact", "--login", "--migrate-config", "--refresh-model-catalog") if flag != "--compact"],
    (["--compact", "session", "--json"], "--compact"),
    *[([flag, mode], flag) for flag in ("--help", "--help-full", "-h") for mode in ("--json", "--json-stream")],
])
def test_json_refusals_name_the_command(argv, missing, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert missing in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--list", "--logins-json", "--models-json", "--providers-json", "--list-model"])
def test_removed_and_abbreviated_flags_are_unknown(flag, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([flag])
    assert exc.value.code == 2
    assert flag in capsys.readouterr().err


def test_agent_listing_has_text_and_json_forms(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    agent = tmp_path / ".js" / "agents" / "local-agent"
    agent.mkdir(parents=True)
    assert cli.main(["--list-agents"]) == 0
    assert "local-agent" in capsys.readouterr().out.splitlines()
    assert cli.main(["--list-agents", "--json"]) == 0
    assert "local-agent" in _stdout_json(capsys)


def test_verify_json(monkeypatch, capsys):
    from js import verify
    monkeypatch.setattr(verify, "verify", lambda agent: ["missing agent"])
    assert cli.main(["--verify", "bad", "--json"]) == 1
    assert _stdout_json(capsys) == {"ok": False, "errors": ["missing agent"]}


def test_text_and_json_model_listings_cover_the_same_cached_routes(monkeypatch, tmp_logins_dir, capsys):
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk"))
    logins.save_login(logins.Login(provider_id="opencode-go", provider_api_key="sk"))
    logins.cache_models("deepseek", ["a"])
    logins.cache_models("opencode-go", ["b"])
    monkeypatch.setattr(cli.logins, "test_login", lambda login: pytest.fail("cached listing went online"))
    cfg = SimpleNamespace(provider_id=None)
    assert listings.print_model_list(None, cfg) == 0
    text = capsys.readouterr().out.splitlines()
    assert listings.models_json(None, cfg) == {"models": text}
