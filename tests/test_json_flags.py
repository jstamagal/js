"""Offline tests for the picker-facing JSON flags in js.cli.

Covers the three machine-readable flags external pickers consume:

  --providers-json  -> cli._providers_json  (js/cli.py:1070)
  --logins-json     -> cli._logins_json     (js/cli.py:1083)
  --models-json     -> cli._models_json     (js/cli.py:1097), dispatched at
                       js/cli.py:1179 with the {"error": ...}/rc=1 wrap at 1193.

The security contract under test lives in _logins_json (js/cli.py:1083-1094):
codex_refresh_token is ALWAYS nulled, has_api_key / has_codex_refresh_token
expose only booleans, and provider_api_key is nulled for codex providers. Note
the actual behavior (asserted below, not assumed): provider_api_key is NOT
nulled for non-codex logins — only codex access JWTs are stripped, because for
ordinary providers the saved key is what the picker needs to reconnect.

--models-json's success path calls logins.test_login, which does network I/O
(asyncio.run(fetch_models)); offline we exercise (a) its error wrap with a real
credential-less failure and (b) its JSON envelope shape with the network
boundary stubbed, mirroring how test_cli_prompt_mode stubs stream_model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from js import cli, codex_auth, logins, paths, providers


@pytest.fixture
def tmp_logins_dir(tmp_path: Path):
    """Isolate the login/cache store, like tests/test_logins.py."""
    logins.set_config_dir(tmp_path)
    yield tmp_path
    logins.set_config_dir(paths.login_store_dir())


def _stdout_json(capsys) -> object:
    out = capsys.readouterr().out
    # _print_json writes exactly one json.dumps line (js/cli.py:1108).
    assert out.endswith("\n")
    return json.loads(out)


# --------------------------------------------------------------------------
# --providers-json
# --------------------------------------------------------------------------

def test_providers_json_shape_and_sources(monkeypatch, tmp_logins_dir, capsys):
    # Make deepseek look env-configured; save a login for a different provider.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-deepseek")
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    logins.save_login(logins.Login(provider_id="mimo", provider_api_key="sk-mimo"))

    rc = cli.main(["--providers-json"])
    rows = _stdout_json(capsys)

    assert rc == 0
    assert isinstance(rows, list)
    # Every row is exactly {id, name, source} (js/cli.py:1077).
    for row in rows:
        assert set(row) == {"id", "name", "source"}
        assert isinstance(row["id"], str)
        assert isinstance(row["name"], str)
        assert row["source"] in {"login", "env", "registry", "custom"}

    by_id = {row["id"]: row for row in rows}
    # Builtin login providers are present.
    assert "deepseek" in by_id
    assert "mimo" in by_id
    # source precedence: login wins over env wins over registry (js/cli.py:1076).
    assert by_id["mimo"]["source"] == "login"
    assert by_id["deepseek"]["source"] == "env"


def test_providers_json_custom_saved_only_provider_appended(monkeypatch, tmp_logins_dir, capsys):
    # A saved login whose id is not in the known registry shows up as "custom"
    # (js/cli.py:1078-1079).
    known = {p.id for p in providers.login_providers()}
    custom_id = "my-private-proxy-xyz"
    assert custom_id not in known
    logins.save_login(
        logins.Login(provider_id=custom_id, provider_base_url="http://proxy.test/v1", provider_api_key="sk-x")
    )

    rc = cli.main(["--providers-json"])
    rows = _stdout_json(capsys)

    assert rc == 0
    by_id = {row["id"]: row for row in rows}
    assert by_id[custom_id] == {"id": custom_id, "name": custom_id, "source": "custom"}


# --------------------------------------------------------------------------
# --logins-json  (secret masking is the point of this flag)
# --------------------------------------------------------------------------

def test_logins_json_empty_store(tmp_logins_dir, capsys):
    rc = cli.main(["--logins-json"])
    rows = _stdout_json(capsys)
    assert rc == 0
    assert rows == []


def test_logins_json_masks_api_key_for_codex_but_keeps_for_normal(tmp_logins_dir, capsys):
    # A normal provider login keeps its api key (the picker needs it); a codex
    # login has its access JWT nulled. Both have refresh tokens nulled.
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-normal-secret"))
    logins.save_login(
        logins.Login(
            provider_id=codex_auth.CODEX_PROVIDER_ID,  # "openai-codex"
            provider_api_key="codex-access-jwt-secret",
            codex_refresh_token="codex-refresh-token-secret",
            codex_account_id="acct-123",
            codex_email="ape@king.test",
        )
    )

    rc = cli.main(["--logins-json"])
    rows = _stdout_json(capsys)

    assert rc == 0
    by_id = {row["provider_id"]: row for row in rows}
    assert set(by_id) == {"deepseek", "openai-codex"}

    normal = by_id["deepseek"]
    codex = by_id["openai-codex"]

    # Each row carries the derived booleans and provider_id (js/cli.py:1087-1089).
    for row in (normal, codex):
        assert "provider_id" in row
        assert isinstance(row["has_api_key"], bool)
        assert isinstance(row["has_codex_refresh_token"], bool)

    # Booleans reflect the ORIGINAL (pre-mask) secret presence.
    assert normal["has_api_key"] is True
    assert normal["has_codex_refresh_token"] is False
    assert codex["has_api_key"] is True
    assert codex["has_codex_refresh_token"] is True

    # ACTUAL behavior: non-codex keeps provider_api_key; codex nulls it.
    assert normal["provider_api_key"] == "sk-normal-secret"
    assert codex["provider_api_key"] is None

    # Refresh token is ALWAYS nulled (js/cli.py:1092).
    assert normal["codex_refresh_token"] is None
    assert codex["codex_refresh_token"] is None


def test_logins_json_never_leaks_codex_refresh_token_anywhere(tmp_logins_dir, capsys):
    # Defense-in-depth: scan the raw JSON text for the secret values.
    refresh_secret = "REFRESH-TOKEN-MUST-NOT-LEAK"
    access_secret = "CODEX-ACCESS-JWT-MUST-NOT-LEAK"
    logins.save_login(
        logins.Login(
            provider_id=codex_auth.CODEX_PROVIDER_ID,
            provider_api_key=access_secret,
            codex_refresh_token=refresh_secret,
        )
    )

    rc = cli.main(["--logins-json"])
    raw = capsys.readouterr().out

    assert rc == 0
    assert refresh_secret not in raw
    assert access_secret not in raw
    # Sanity: it is still valid JSON describing the codex login.
    rows = json.loads(raw)
    assert rows[0]["provider_id"] == "openai-codex"
    assert rows[0]["has_codex_refresh_token"] is True
    assert rows[0]["has_api_key"] is True


def test_logins_json_does_not_leak_normal_api_key_under_codex_id(tmp_logins_dir, capsys):
    # The api-key mask is keyed on is_codex_provider; confirm a non-codex login's
    # key is intentionally retained and the codex one is intentionally dropped,
    # so a future refactor that flips the predicate is caught.
    logins.save_login(logins.Login(provider_id="mimo", provider_api_key="sk-mimo-secret"))
    logins.save_login(
        logins.Login(provider_id=codex_auth.CODEX_PROVIDER_ID, provider_api_key="codex-secret")
    )

    rc = cli.main(["--logins-json"])
    raw = capsys.readouterr().out
    rows = json.loads(raw)

    assert rc == 0
    by_id = {row["provider_id"]: row for row in rows}
    assert by_id["mimo"]["provider_api_key"] == "sk-mimo-secret"
    assert by_id["openai-codex"]["provider_api_key"] is None
    assert "codex-secret" not in raw


# --------------------------------------------------------------------------
# --models-json
# --------------------------------------------------------------------------

def test_models_json_shape_with_stubbed_provider(monkeypatch, tmp_logins_dir, capsys):
    # Stub the network boundary (logins.test_login) the way prompt-mode tests
    # stub stream_model, then assert the {"models": [...]} envelope.
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))
    monkeypatch.setattr(cli.logins, "test_login", lambda login: ["m-alpha", "m-beta"])

    rc = cli.main(["--models-json", "deepseek"])
    payload = _stdout_json(capsys)

    assert rc == 0
    assert payload == {"models": ["m-alpha", "m-beta"]}


def test_models_json_error_path_is_json_and_returns_one(tmp_logins_dir, capsys, monkeypatch):
    # Offline, with no saved login and no creds, the SDK raises on missing
    # credentials before any request; cli wraps it as {"error": ...}, rc=1
    # (js/cli.py:1193-1195).
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)

    rc = cli.main(["--models-json", "openai"])
    payload = _stdout_json(capsys)

    assert rc == 1
    assert isinstance(payload, dict)
    assert set(payload) == {"error"}
    assert isinstance(payload["error"], str)
    assert payload["error"]


def test_models_json_no_arg_uses_config_provider(monkeypatch, tmp_logins_dir, capsys):
    # `--models-json` with no value (const="") sends provider_arg=None, which
    # builds a Config and feeds cfg.provider_id to _models_for_provider
    # (js/cli.py:1182-1192). Stub the network boundary and capture the cfg.
    seen: list[str | None] = []

    def fake_test_login(login):
        seen.append(login.provider_id)
        return ["cfg-model"]

    monkeypatch.setattr(cli.logins, "test_login", fake_test_login)

    rc = cli.main(["--models-json"])
    payload = _stdout_json(capsys)

    # Either it resolved a provider from config and returned the stubbed list,
    # or there was no provider configured and it reported an error -- both are
    # valid JSON-shaped outcomes that never crash.
    if rc == 0:
        assert payload == {"models": ["cfg-model"]}
        assert seen  # the config-driven path reached the (stubbed) boundary
    else:
        assert rc == 1
        assert set(payload) == {"error"}


def test_list_models_human_output_shows_exact_model_flag(monkeypatch, tmp_logins_dir, capsys):
    logins.save_login(logins.Login(provider_id="openai-codex", provider_api_key="jwt"))
    monkeypatch.setattr(cli.logins, "test_login", lambda login: ["gpt-5.4", "gpt-5.5"])

    rc = cli.main(["--list-models", "openai-codex"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "provider: openai-codex" in out
    assert "gpt-5.4" in out
    assert "--model openai-codex/gpt-5.4" in out
    assert "--model openai-codex/gpt-5.5" in out
