"""logins.toml hand edits: a malformed section must never crash a read, and
must never be silently dropped by the next write (ALL-CLAIMS logins.py:216)."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

from js import logins, paths


@pytest.fixture
def store(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    yield tmp_path
    logins.set_config_dir(paths.login_store_dir())


def test_load_logins_survives_wrong_types_and_quarantines_the_section(store, capsys):
    (store / "logins.toml").write_text(
        "[probe]\nprovider_id = 5\nprovider_api_key = 5\n"
        "[good]\nprovider_id = \"good\"\nprovider_api_key = \"k\"\n",
        encoding="utf-8",
    )
    loaded = logins.load_logins()  # must not raise (routing/picker call this on every turn)
    assert "probe" not in loaded
    assert loaded["good"].provider_api_key == "k"
    assert "probe" in capsys.readouterr().err


def test_save_login_keeps_a_quarantined_section_verbatim(store):
    (store / "logins.toml").write_text(
        "[probe]\nprovider_id = 5\nfuture_field = \"keep me\"\n",
        encoding="utf-8",
    )
    logins.load_logins()
    logins.save_login(logins.Login(provider_id="other", provider_api_key="k"))
    data = tomllib.loads((store / "logins.toml").read_text(encoding="utf-8"))
    assert data["probe"] == {"provider_id": 5, "future_field": "keep me"}
    assert data["other"]["provider_api_key"] == "k"


def test_remove_login_keeps_a_quarantined_section_verbatim(store):
    (store / "logins.toml").write_text(
        "[probe]\nprovider_id = 5\n[other]\nprovider_id = \"other\"\nprovider_api_key = \"k\"\n",
        encoding="utf-8",
    )
    assert logins.remove_login("other") is True
    data = tomllib.loads((store / "logins.toml").read_text(encoding="utf-8"))
    assert data == {"probe": {"provider_id": 5}}


def test_unknown_keys_in_a_good_section_round_trip(store):
    # A newer js may write a field this checkout does not know; saving another
    # login must not strip it.
    (store / "logins.toml").write_text(
        "[good]\nprovider_id = \"good\"\nprovider_api_key = \"k\"\nnew_field = 1\n",
        encoding="utf-8",
    )
    logins.save_login(logins.Login(provider_id="other", provider_api_key="k2"))
    data = tomllib.loads((store / "logins.toml").read_text(encoding="utf-8"))
    assert data["good"]["new_field"] == 1
    assert data["good"]["provider_api_key"] == "k"


def test_unparseable_expiry_reads_as_expired_not_crash(store):
    (store / "logins.toml").write_text(
        "[openai-codex]\nprovider_id = \"openai-codex\"\nprovider_api_key = \"a\"\n"
        "codex_refresh_token = \"r\"\ncodex_token_expires = \"soon\"\n"
        "[xai-oauth]\nprovider_id = \"xai-oauth\"\nprovider_api_key = \"a\"\n"
        "xai_refresh_token = \"r\"\nxai_token_expires = true\n",
        encoding="utf-8",
    )
    loaded = logins.load_logins()
    assert loaded["openai-codex"].codex_token_expires is None
    assert loaded["xai-oauth"].xai_token_expires is None
    from js import codex_auth, xai_auth

    assert codex_auth.login_needs_refresh(loaded["openai-codex"]) is True
    assert xai_auth.login_needs_refresh(loaded["xai-oauth"]) is True


def test_numeric_string_expiry_is_parsed(store):
    (store / "logins.toml").write_text(
        "[openai-codex]\nprovider_id = \"openai-codex\"\ncodex_token_expires = \"1700000000.5\"\n",
        encoding="utf-8",
    )
    assert logins.load_logins()["openai-codex"].codex_token_expires == 1700000000.5


def test_bad_headers_table_is_quarantined(store):
    (store / "logins.toml").write_text(
        "[probe]\nprovider_id = \"probe\"\nprovider_headers = \"not a table\"\n",
        encoding="utf-8",
    )
    assert "probe" not in logins.load_logins()


@pytest.mark.parametrize("literal", ["nan", "inf", "-inf", "true", "\"nan\"", "\"inf\""])
def test_nonfinite_or_bool_expiry_reads_as_expired(store, literal):
    # TOML accepts nan/inf as floats; `now >= nan` is False, so a nan expiry
    # would never refresh ("fresh forever"). Unknown must mean expired.
    (store / "logins.toml").write_text(
        f"[openai-codex]\nprovider_id = \"openai-codex\"\nprovider_api_key = \"a\"\n"
        f"codex_refresh_token = \"r\"\ncodex_token_expires = {literal}\n",
        encoding="utf-8",
    )
    from js import codex_auth

    login = logins.load_logins()["openai-codex"]
    assert login.codex_token_expires is None
    assert codex_auth.login_needs_refresh(login) is True


def test_remove_login_of_a_quarantined_section_leaves_it_in_place(store):
    # remove_login works on usable logins; a quarantined table is not one of
    # them, so it reports "did not exist" and the bytes stay for the human.
    (store / "logins.toml").write_text("[probe]\nprovider_id = 5\n", encoding="utf-8")
    assert logins.remove_login("probe") is False
    data = tomllib.loads((store / "logins.toml").read_text(encoding="utf-8"))
    assert data == {"probe": {"provider_id": 5}}


def test_non_table_top_level_value_is_quarantined(store):
    (store / "logins.toml").write_text(
        "stray = 1\n[good]\nprovider_id = \"good\"\nprovider_api_key = \"k\"\n",
        encoding="utf-8",
    )
    loaded = logins.load_logins()
    assert list(loaded) == ["good"]
    logins.save_login(logins.Login(provider_id="other", provider_api_key="k2"))
    data = tomllib.loads((store / "logins.toml").read_text(encoding="utf-8"))
    assert data["stray"] == 1
