"""Contract for `js -u [wire+]scheme://model[:key]@host/path` and JS_BASE_URL."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from js import cli, logins, runtime, settings
from js.config import from_env
from js.endpoint_uri import DUMMY_KEY, EndpointSpecError, from_env as endpoint_from_env, parse
from js.logins import Login
from js.turn_call import ModelRequest

LOGIN_BASE = "http://localhost:8050/v1"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setattr(logins, "_CONFIG_DIR_OVERRIDE", tmp_path / "login-store")
    for name in ("JS_MODEL", "JS_PROVIDER", "JS_BASE_URL", "JS_API_KEY"):
        monkeypatch.delenv(name, raising=False)


# ---- the ledger's examples ----------------------------------------------------


@pytest.mark.parametrize("spec, model, key, base, provider", [
    ("http://qwen3:27b@box:11434/v1",
     "qwen3:27b", DUMMY_KEY, "http://box:11434/v1", "openai-completions"),
    ("http://qwen3.8-27b:sk-foo-bar@box/v1",
     "qwen3.8-27b", "sk-foo-bar", "http://box/v1", "openai-completions"),
    ("https://nex-agi/nex-n2.5-pro:free:sk-or-xxxx@openrouter.ai/api/v1",
     "nex-agi/nex-n2.5-pro:free", "sk-or-xxxx", "https://openrouter.ai/api/v1", "openai-completions"),
    ("anthropic+https://deepseek-v4-pro:sk-xxxx@api.deepseek.com/anthropic",
     "deepseek-v4-pro", "sk-xxxx", "https://api.deepseek.com/anthropic", "anthropic-custom"),
])
def test_ledger_examples(spec, model, key, base, provider):
    got = parse(spec)
    assert (got.model, got.api_key, got.base_url, got.provider_id) == (model, key, base, provider)


# ---- wire --------------------------------------------------------------------


@pytest.mark.parametrize("prefix, provider", [
    ("openai+", "openai-completions"),
    ("responses+", "openai-responses"),
    ("anthropic+", "anthropic-custom"),
])
def test_an_explicit_wire_prefix_wins_over_the_path(prefix, provider):
    assert parse(f"{prefix}http://m@h/v1").provider_id == provider
    assert parse(f"{prefix}http://m@h/anthropic").provider_id == provider


def test_the_prefix_is_stripped_from_the_base_url():
    assert parse("responses+https://m@gw.example/v1").base_url == "https://gw.example/v1"


@pytest.mark.parametrize("path, provider", [
    ("/v1", "openai-completions"),
    ("/api/v1", "openai-completions"),
    ("/v1/", "openai-completions"),
    ("", "anthropic-custom"),
    ("/anthropic", "anthropic-custom"),
    ("/v1beta", "anthropic-custom"),
])
def test_without_a_prefix_a_v1_path_is_openai_and_anything_else_anthropic(path, provider):
    assert parse(f"http://m@box:8317{path}").provider_id == provider


def test_an_unknown_wire_prefix_is_named():
    with pytest.raises(EndpointSpecError, match="bogus"):
        parse("bogus+http://m@h/v1")


def test_unknown_wire_is_exit_2(capsys):
    assert cli.main(["-u", "bogus+http://m@h/v1", "-p", "hi"]) == 2
    assert "bogus" in capsys.readouterr().err


# ---- model and key ------------------------------------------------------------


@pytest.mark.parametrize("tag", ["free", "27b", "Q4_K_M", "auto"])
def test_a_tag_after_the_last_colon_stays_in_the_model(tag):
    got = parse(f"http://qwen3:{tag}@box/v1")
    assert got.model == f"qwen3:{tag}"
    assert got.api_key == DUMMY_KEY


def test_slashes_in_the_model_are_fine():
    assert parse("http://antigravity/gemini-3.8-flash@vader:8317/v1").model == "antigravity/gemini-3.8-flash"


def test_the_last_at_splits_model_from_host():
    got = parse("http://team@corp/m:sk-abcdefgh@h/v1")
    assert got.model == "team@corp/m"
    assert got.api_key == "sk-abcdefgh"
    assert got.base_url == "http://h/v1"


def test_a_key_the_detector_misses_comes_from_js_api_key():
    got = parse("http://m@h/v1", env={"JS_API_KEY": "plainword"})
    assert got.model == "m"
    assert got.api_key == "plainword"


def test_no_key_is_the_endpoint_default_key():
    assert parse("http://m@h/v1", settings={"ENDPOINT_DEFAULT_KEY": "local-key"}).api_key == "local-key"


def test_no_at_takes_the_model_from_js_model():
    assert parse("http://box/v1", env={"JS_MODEL": "qwen3:27b"}).model == "qwen3:27b"


def test_no_at_and_no_js_model_is_an_error():
    with pytest.raises(EndpointSpecError, match="JS_MODEL"):
        parse("http://box/v1")
    assert cli.main(["-u", "http://box/v1", "-p", "hi"]) == 2


def test_the_flag_beats_js_model_and_js_api_key():
    got = parse("http://m:sk-abcdefgh@h/v1", env={"JS_MODEL": "other", "JS_API_KEY": "k2"})
    assert (got.model, got.api_key) == ("m", "sk-abcdefgh")


def test_settings_are_emitted_in_the_form_extra_accepts():
    assert parse("responses+https://m:sk-abcdefgh@h/v1").as_settings() == [
        "MODEL=m",
        "PROVIDER=openai-responses",
        "BASE_URL=https://h/v1",
        "API_KEY=sk-abcdefgh",
    ]


# ---- refusals -----------------------------------------------------------------


@pytest.mark.parametrize("spec", [
    "http://m[anthropic]@h/v1",
    "http://m[max]@h/v1",
    "http://m:sk-abcdefgh[responses][high]@h/v1",
    "m[openai]@http://h/v1",
])
def test_a_bracket_is_refused(spec):
    with pytest.raises(EndpointSpecError, match=r"-r"):
        parse(spec)
    assert cli.main(["-u", spec, "-p", "hi"]) == 2


@pytest.mark.parametrize("spec", [
    "",
    "qwen27b@box/v1",
    "ftp://m@h/v1",
    "http://@h/v1",
    "http://m:@h/v1",
    "http://m@",
    "http://m@/v1",
])
def test_unreadable_specs_are_errors(spec):
    with pytest.raises(EndpointSpecError):
        parse(spec)


def test_an_error_never_prints_the_key():
    with pytest.raises(EndpointSpecError) as exc:
        parse("ftp://m:sk-abcdefgh@h/v1")
    assert "sk-abcdefgh" not in str(exc.value)


# ---- banner --------------------------------------------------------------------


def _banner(monkeypatch, *, provider_id, api_key, settings_=None):
    seen = {}
    monkeypatch.setattr(runtime, "_output", lambda _t, name, **f: seen.update(f))
    monkeypatch.setattr(runtime.compaction, "configured_context_window", lambda *a: None)
    request = ModelRequest(model="m", provider_id=provider_id, base_url="http://h/v1",
                           api_key=api_key, effort=None, max_out=None, settings=settings_ or {})
    registry = SimpleNamespace(openai_specs=lambda: [])
    context = SimpleNamespace(config=None, vision_enabled=False)
    runtime._trace_banner(None, request, context, registry, lambda: None)
    return seen["fields"]


@pytest.mark.parametrize("provider_id, wire", [
    ("openai-completions", "openai"),
    ("openai-responses", "responses"),
    ("anthropic-custom", "anthropic"),
    (None, "openai"),
])
def test_banner_names_model_key_and_wire(monkeypatch, provider_id, wire):
    fields = _banner(monkeypatch, provider_id=provider_id, api_key="sk-secret-value")
    assert "model=m" in fields
    assert "key=set" in fields
    assert f"wire={wire}" in fields
    assert not any("sk-secret-value" in f for f in fields)


@pytest.mark.parametrize("api_key", [None, "", DUMMY_KEY])
def test_banner_says_key_none_without_a_real_key(monkeypatch, api_key):
    assert "key=none" in _banner(monkeypatch, provider_id="openai-completions", api_key=api_key)


# ---- JS_BASE_URL -------------------------------------------------------------------


def test_js_base_url_takes_the_same_form(monkeypatch, tmp_path):
    monkeypatch.setenv("JS_BASE_URL", "anthropic+https://deepseek-v4-pro:sk-xxxx@api.deepseek.com/anthropic")
    cfg = from_env(cwd=tmp_path, save_session=False)
    assert cfg.model == "deepseek-v4-pro"
    assert cfg.provider_id == "anthropic-custom"
    assert cfg.provider_base_url == "https://api.deepseek.com/anthropic"
    assert cfg.provider_api_key == "sk-xxxx"


def test_js_base_url_uses_the_wire_rule_and_js_model(monkeypatch, tmp_path):
    monkeypatch.setenv("JS_BASE_URL", "http://qwen3:27b@box:11434/v1")
    cfg = from_env(cwd=tmp_path, save_session=False)
    assert (cfg.model, cfg.provider_id, cfg.provider_base_url) == (
        "qwen3:27b", "openai-completions", "http://box:11434/v1")
    assert cfg.provider_api_key == DUMMY_KEY


def test_js_base_url_wire_prefix_with_js_model_and_js_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("JS_BASE_URL", "responses+https://gw.example/v1")
    monkeypatch.setenv("JS_MODEL", "gpt-x")
    monkeypatch.setenv("JS_API_KEY", "plainword")
    cfg = from_env(cwd=tmp_path, save_session=False)
    assert (cfg.model, cfg.provider_id, cfg.provider_base_url, cfg.provider_api_key) == (
        "gpt-x", "openai-responses", "https://gw.example/v1", "plainword")


def test_matching_js_variables_are_fine():
    got = endpoint_from_env({"JS_BASE_URL": "anthropic+https://m:sk-abcdefgh@h/x",
                             "JS_MODEL": "m", "JS_API_KEY": "sk-abcdefgh",
                             "JS_PROVIDER": "anthropic-custom"})
    assert (got.model, got.api_key, got.provider_id) == ("m", "sk-abcdefgh", "anthropic-custom")


def test_js_provider_picks_the_wire_when_the_url_has_no_prefix():
    got = endpoint_from_env({"JS_BASE_URL": "https://m@h/v1", "JS_PROVIDER": "openai-responses"})
    assert got.provider_id == "openai-responses"


def test_a_plain_js_base_url_is_not_rewritten():
    assert endpoint_from_env({"JS_BASE_URL": "https://omp.test/v1", "JS_MODEL": "m"}) is None


@pytest.mark.parametrize("env, needles", [
    ({"JS_BASE_URL": "http://m1@h/v1", "JS_MODEL": "m2"}, ("m1", "m2", "JS_MODEL")),
    ({"JS_BASE_URL": "http://m:sk-abcdefgh@h/v1", "JS_API_KEY": "sk-zzzzzzzz"}, ("JS_API_KEY",)),
    ({"JS_BASE_URL": "anthropic+http://m@h/v1", "JS_PROVIDER": "openai-completions"},
     ("anthropic", "openai-completions", "JS_PROVIDER")),
    ({"JS_BASE_URL": "responses+http://h/v1"}, ("JS_MODEL",)),
    ({"JS_BASE_URL": "http://m[max]@h/v1"}, ("-r",)),
])
def test_js_base_url_contradictions_name_both_and_exit_2(monkeypatch, capsys, env, needles):
    with pytest.raises(EndpointSpecError) as exc:
        endpoint_from_env(env)
    for needle in needles:
        assert needle in str(exc.value)
    assert "sk-abcdefgh" not in str(exc.value) and "sk-zzzzzzzz" not in str(exc.value)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    capsys.readouterr()
    assert cli.main(["-p", "hi"]) == 2
    assert needles[-1] in capsys.readouterr().err


def test_an_unreadable_js_base_url_is_skipped_by_the_env_layer(monkeypatch):
    collected = settings.apply_env_overrides({"BASE_URL": "http://jsrc/v1"},
                                             env={"JS_BASE_URL": "bogus+http://m@h/v1"})
    assert collected["BASE_URL"] == "http://jsrc/v1"


def test_model_flag_with_a_login_beats_an_exported_endpoint_js_base_url(monkeypatch, tmp_path):
    logins.save_login(Login(provider_id="testes", sdk_provider_id="openai",
                            provider_base_url=LOGIN_BASE, provider_api_key="x"))
    monkeypatch.setenv("JS_BASE_URL", "http://qwen3:27b@stray.test/v1")

    actual = cli._resolve_cli_model_override(from_env(cwd=tmp_path, save_session=False), "testes/other")

    assert actual.provider_id == "testes"
    assert actual.model == "other"
    assert actual.provider_base_url == LOGIN_BASE
    assert actual.provider_api_key == "x"
