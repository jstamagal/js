"""Custom `js --login` providers must be first-class routing citizens.

A custom login (a local llama.cpp box, say) is visible to /model and
--list-models and to routing. It used to be invisible to routing:
`provider/model` prefixes shipped the WHOLE string to the default provider,
and pinning `PROVIDER` resolved base_url but handed the raw js id to the SDK
("unknown provider id").
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from repl_driver import record_turns, run_async, turn_stub
from turn_driver import js, one_turn

from js import (
    cli,
    login_cli,
    login_defaults,
    logins,
    messages as msgs,
    model_metadata,
    providers,
    repl_model_commands,
    repl_settings_commands,
    run_assembly,
    setting_values,
)
from js.config import from_env
from js.logins import Login
from js.repl_state import ReplState
from js.routing import ProviderNotLoggedInError, resolve_model_route

BASE_URL = "http://localhost:8050/v1"
GGUF = str(Path.home() / ".cache/huggingface/models/ornith-1.0-35b-Q5_K_M.gguf")


@pytest.fixture(autouse=True)
def isolated_login_store(monkeypatch, tmp_path):
    monkeypatch.setattr(logins, "_CONFIG_DIR_OVERRIDE", tmp_path / "login-store")


def _save_custom_login(provider_id: str, sdk: str = "openai", shape: str | None = None) -> None:
    logins.save_login(
        Login(
            provider_id=provider_id,
            sdk_provider_id=sdk,
            shape_provider_id=shape,
            provider_base_url=BASE_URL,
            provider_api_key="x",
        )
    )


def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    home = tmp_path / "home"
    config_home = home / ".config"
    data_home = home / ".local" / "share"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    for name in ("JS_MODEL", "JS_PROVIDER", "JS_BASE_URL", "JS_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_get_provider_synthesizes_def_from_saved_login():
    _save_custom_login("testes")
    provider = providers.get_provider("testes")
    assert provider is not None
    assert provider.id == "testes"
    assert provider.transport == "custom_openai"
    assert provider.effective_sdk_provider_id == "openai"
    assert provider.default_base_url == BASE_URL


def test_anthropic_shape_login_gets_anthropic_transport():
    _save_custom_login("myclaude", sdk="anthropic")
    provider = providers.get_provider("myclaude")
    assert provider is not None
    assert provider.transport == "custom_anthropic"


def test_openai_responses_shape_login_gets_responses_transport():
    _save_custom_login("myresponses", sdk="openai", shape="openai-responses")
    provider = providers.get_provider("myresponses")
    assert provider is not None
    assert provider.transport == "custom_responses"
    assert provider.effective_sdk_provider_id == "openai"


def test_prefix_splits_on_saved_login():
    _save_custom_login("testes")
    assert providers.parse_model_prefix("testes/test") == ("testes", "test")


def test_prefix_splits_on_saved_login_with_path_model():
    _save_custom_login("booty")
    assert providers.parse_model_prefix(f"booty/{GGUF}") == ("booty", GGUF)


def test_malformed_provider_prefix_does_not_fall_back_to_default():
    _save_custom_login("cpa")
    route = resolve_model_route(
        "cpa/hyper/Qwen3.8-2.4T-A95B",
        configured_provider_id="deepseek",
        env={},
        explicit_model=True,
    )
    assert route.provider_id == "cpa"
    assert route.model == "hyper/Qwen3.8-2.4T-A95B"
    with pytest.raises(ValueError):
        resolve_model_route(
            "'cpa/gpt-6.1-sol",
            configured_provider_id="deepseek",
            env={},
            explicit_model=True,
        )


def test_unknown_prefix_still_passes_through_whole():
    assert providers.parse_model_prefix("nope/some-model") == (None, "nope/some-model")


def test_route_by_prefix_carries_login_endpoint():
    _save_custom_login("testes")
    route = resolve_model_route("testes/test", env={}, explicit_model=True)
    assert route.provider_id == "testes"
    assert route.model == "test"
    assert route.base_url == BASE_URL
    assert route.api_key == "x"
    assert route.transport == "custom_openai"


@pytest.mark.parametrize("model", [
    "google/gemini-4-neon",
    "baidu/ernie-5.5-flash-preview:free",
    "google/gemini-4-neon:batch",
])
def test_openrouter_cli_routes_with_only_its_env_key(monkeypatch, tmp_path, model):
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-env-key")
    calls = []

    def turn(cfg, system, messages, *_args, **kwargs):
        calls.append(kwargs)
        messages.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli.runtime, "run_turn_async", turn_stub(turn))
    assert js(monkeypatch, "-m", f"openrouter/{model}", "-p", "hi") == 0

    [call] = calls
    assert call["provider_id_override"] == "openrouter"
    assert call["model_override"] == model
    assert call["provider_base_url_override"] == "https://openrouter.ai/api/v1"
    assert call["provider_api_key_override"] == "openrouter-env-key"


def test_openrouter_without_credentials_requires_login():
    with pytest.raises(ProviderNotLoggedInError):
        resolve_model_route("openrouter/google/gemini-4-neon", env={})


def test_pinned_openrouter_uses_its_endpoint_and_model_env():
    route = resolve_model_route("default", configured_provider_id="openrouter", env={
        "OPENROUTER_BASE_URL": BASE_URL,
        "OPENROUTER_MODEL": "google/gemini-4-neon:batch",
    })
    assert route.base_url == BASE_URL
    assert route.model == "google/gemini-4-neon:batch"


def test_openrouter_lists_models_using_its_env_key(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-env-key")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    model = "google/gemini-4-neon:batch"
    assert [row[0] for row in login_cli._login_provider_rows()].count("openrouter") == 1

    def list_models(login):
        assert login.effective_provider_id == "openai"
        assert login.provider_base_url == "https://openrouter.ai/api/v1"
        assert login.provider_api_key == "openrouter-env-key"
        return [model]

    monkeypatch.setattr(logins, "test_login", list_models)
    assert js(monkeypatch, "--list-models", "openrouter", "--json") == 0
    assert json.loads(capsys.readouterr().out)["models"] == [f"openrouter/{model}"]


def test_openrouter_uses_its_catalog_limits_and_prices():
    model = "openrouter/google/gemini-2.5-flash"
    limits = model_metadata.lookup_limits(model)
    assert limits is not None
    assert limits.provider_id == "openrouter"
    assert limits.context_window == 1_048_576
    assert model_metadata.model_prices(model)


def test_openrouter_saved_login_uses_builtin_and_appears_once(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-env-key")
    base_url = "https://openrouter.ai/api/v1"
    model = "baidu/ernie-5.5-flash-preview:free"
    logins.save_login(Login(
        provider_id="openrouter",
        sdk_provider_id="openai",
        shape_provider_id="openai-completions",
        provider_base_url=base_url,
        provider_api_key="openrouter-saved-key",
    ))
    logins.cache_models("openrouter", [model])

    route = resolve_model_route(f"openrouter/{model}", explicit_model=True)
    assert (route.provider_id, route.model) == ("openrouter", model)
    assert (route.base_url, route.api_key) == (base_url, "openrouter-saved-key")
    assert route.transport == "openai_compatible"

    assert js(monkeypatch, "--list-providers", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    [provider] = [row for row in rows if row["id"] == "openrouter"]
    assert provider["source"] == "login"
    assert [row[0] for row in login_cli._login_provider_rows()].count("openrouter") == 1

    assert js(monkeypatch, "--list-models", "openrouter", "--json") == 0
    assert json.loads(capsys.readouterr().out)["models"] == [f"openrouter/{model}"]


def test_login_prefix_overrides_pinned_provider():
    _save_custom_login("testes")
    route = resolve_model_route(
        "testes/test",
        configured_provider_id="deepseek",
        env={},
        explicit_model=True,
    )
    assert route.provider_id == "testes"
    assert route.model == "test"
    assert route.base_url == BASE_URL


def test_pinned_custom_provider_resolves_transport_and_endpoint():
    _save_custom_login("testes")
    route = resolve_model_route(
        "test",
        configured_provider_id="testes",
        env={},
        explicit_model=True,
    )
    assert route.provider_id == "testes"
    assert route.model == "test"
    assert route.base_url == BASE_URL
    assert route.transport == "custom_openai"


def test_known_provider_ids_includes_saved_logins():
    _save_custom_login("testes")
    assert "testes" in providers.known_provider_ids()


def test_explicit_model_known_unlogged_prefix_does_not_ride_saved_default(
    monkeypatch, tmp_path
):
    _isolate_config(monkeypatch, tmp_path)
    _save_custom_login("testes")
    config_file = tmp_path / "home" / ".js" / "jsrc"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("set MODEL testes/test\n", encoding="utf-8")
    monkeypatch.setenv("HF_TOKEN", "hf-decoy")

    cfg = from_env(cwd=tmp_path, save_session=False)
    assert cfg.provider_id == "testes"
    assert cfg.model == "test"

    with pytest.raises(ProviderNotLoggedInError) as excinfo:
        run_assembly.route(cfg, "huggingface/foo")

    assert str(excinfo.value) == msgs.PROVIDER_NOT_LOGGED_IN.text(provider="huggingface")


def test_js_model_known_unlogged_prefix_does_not_ride_saved_default(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _save_custom_login("testes")
    config_file = tmp_path / "home" / ".js" / "jsrc"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("set MODEL testes/test\n", encoding="utf-8")
    monkeypatch.setenv("JS_MODEL", "huggingface/foo")
    monkeypatch.setenv("HF_TOKEN", "hf-decoy")

    with pytest.raises(ProviderNotLoggedInError) as excinfo:
        from_env(cwd=tmp_path, save_session=False)

    assert str(excinfo.value) == msgs.PROVIDER_NOT_LOGGED_IN.text(provider="huggingface")


def test_explicit_gateway_provider_keeps_known_provider_model_id(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("JS_PROVIDER", "omp")
    monkeypatch.setenv("JS_BASE_URL", "https://omp.test/v1")
    monkeypatch.setenv("JS_MODEL", "anthropic/claude-x")

    cfg = from_env(cwd=tmp_path, save_session=False)

    assert cfg.provider_id == "omp"
    assert cfg.model == "anthropic/claude-x"
    assert cfg.provider_base_url == "https://omp.test/v1"
    assert cfg.explicit_provider is True


def test_cli_model_override_keeps_known_provider_id_under_explicit_gateway(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("JS_PROVIDER", "omp")
    monkeypatch.setenv("JS_BASE_URL", "https://omp.test/v1")

    cfg = from_env(cwd=tmp_path, save_session=False)
    actual = run_assembly.route(cfg, "anthropic/claude-x")

    assert actual.provider_id == "omp"
    assert actual.model == "anthropic/claude-x"
    assert actual.provider_base_url == "https://omp.test/v1"


def test_explicit_gateway_provider_still_yields_to_saved_login_prefix(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _save_custom_login("testes")
    monkeypatch.setenv("JS_PROVIDER", "omp")
    monkeypatch.setenv("JS_BASE_URL", "https://omp.test/v1")
    monkeypatch.setenv("JS_MODEL", "testes/test")

    cfg = from_env(cwd=tmp_path, save_session=False)

    assert cfg.provider_id == "testes"
    assert cfg.model == "test"
    assert cfg.provider_base_url == BASE_URL
    assert cfg.provider_api_key == "x"


def test_explicit_model_same_saved_prefix_routes_to_saved_login(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _save_custom_login("testes")
    config_file = tmp_path / "home" / ".js" / "jsrc"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("set MODEL testes/test\n", encoding="utf-8")

    cfg = from_env(cwd=tmp_path, save_session=False)
    actual = run_assembly.route(cfg, "testes/other")

    assert actual.provider_id == "testes"
    assert actual.model == "other"
    assert actual.provider_base_url == BASE_URL
    assert actual.provider_api_key == "x"


def test_model_flag_to_another_provider_does_not_ride_a_stray_base_url(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    logins.save_login(Login(provider_id="alpha", sdk_provider_id="openai",
                            provider_base_url="http://alpha.test/v1", provider_api_key="a"))
    _save_custom_login("testes")
    config_file = tmp_path / "home" / ".js" / "jsrc"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("set MODEL alpha/m\n", encoding="utf-8")
    monkeypatch.setenv("JS_BASE_URL", "http://stray.test/v1")

    actual = run_assembly.route(from_env(cwd=tmp_path, save_session=False), "testes/other")

    assert actual.provider_id == "testes"
    assert actual.provider_base_url == BASE_URL
    assert actual.provider_api_key == "x"


def test_pinned_gateway_unknown_prefix_passes_through_whole_model_id():
    route = resolve_model_route(
        "unknown-vendor/model-name",
        configured_provider_id="omp",
        configured_base_url="https://gateway.test/v1",
        configured_api_key="sk-omp",
        env={},
        explicit_model=True,
        prefix_overrides_provider=True,
    )

    assert route.provider_id == "omp"
    assert route.model == "unknown-vendor/model-name"
    assert route.base_url == "https://gateway.test/v1"
    assert route.api_key == "sk-omp"


@pytest.mark.parametrize("pin", [None, "deepseek"])
def test_login_routed_prefix_carries_login_even_when_caller_opts_out(pin):
    # ReplState.config_for_turn routes with use_saved_login=False; a builtin-provider
    # prefix that routes because of its login must still go to the login's
    # endpoint, not the provider default with an env key.
    logins.save_login(Login(provider_id="deepseek", provider_base_url="https://private.test/v1",
                            provider_api_key="LOGIN-KEY"))
    route = resolve_model_route(
        "deepseek/model-x",
        configured_provider_id=pin,
        env={"DEEPSEEK_API_KEY": "ENV-KEY"},
        explicit_model=True,
        use_saved_login=False,
    )
    assert route.provider_id == "deepseek"
    assert route.base_url == "https://private.test/v1"
    assert route.api_key == "LOGIN-KEY"


def test_same_pin_opt_out_keeps_explicit_base_and_key():
    logins.save_login(Login(provider_id="deepseek", provider_base_url="https://private.test/v1",
                            provider_api_key="LOGIN-KEY"))
    route = resolve_model_route(
        "deepseek/model-x",
        configured_provider_id="deepseek",
        configured_base_url="https://override.test/v1",
        configured_api_key="OVERRIDE",
        env={},
        explicit_model=True,
        use_saved_login=False,
    )
    assert route.base_url == "https://override.test/v1"
    assert route.api_key == "OVERRIDE"


def test_js_base_url_boot_takes_the_saved_server_and_model_defaults(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    logins.save_login(Login(
        provider_id="testes", sdk_provider_id="openai", provider_base_url=BASE_URL, provider_api_key="x",
        defaults={"CONTEXT_SIZE": 32768, "TEMPERATURE": 0.2, "OUTPUT_TOKENS_MAX": 1000},
        model_defaults={"served": {"TEMPERATURE": 0.6, "OUTPUT_TOKENS_MAX": 2000}},
    ))
    monkeypatch.setenv("JS_BASE_URL", BASE_URL + "/")
    monkeypatch.setenv("JS_MODEL", "served")
    monkeypatch.setenv("JS_OUTPUT_TOKENS_MAX", "3000")

    cfg = from_env(cwd=tmp_path, save_session=False)

    assert cfg.model_context_window == 32768          # server
    assert cfg.settings["TEMPERATURE"] == 0.6          # model over server
    assert login_defaults.sampling(cfg).temperature == 0.6
    assert cfg.max_output_tokens == 3000               # env over model


def test_save_server_writes_the_session_model_settings_into_the_login(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _save_custom_login("testes")
    monkeypatch.setenv("JS_MODEL", "testes/served")
    cfg = from_env(cwd=tmp_path, save_session=False)

    run_async(monkeypatch, cfg, ["/set TEMPERATURE 0.7", "/set CONTEXT_SIZE 65536", "/save server"])

    saved = logins.load_logins_fresh()["testes"].model_defaults["served"]
    assert saved["TEMPERATURE"] == 0.7
    assert saved["CONTEXT_SIZE"] == 65536


def test_a_model_chosen_after_startup_takes_its_own_login_defaults(monkeypatch, tmp_path):
    from js import runtime

    _isolate_config(monkeypatch, tmp_path)
    logins.save_login(Login(
        provider_id="testes", sdk_provider_id="openai", provider_base_url=BASE_URL, provider_api_key="x",
        defaults={"CONTEXT_SIZE": 32768},
        model_defaults={"a": {"TEMPERATURE": 0.2}, "b": {"TEMPERATURE": 0.9, "CONTEXT_SIZE": 131072}},
    ))
    monkeypatch.setenv("JS_MODEL", "testes/a")
    monkeypatch.setenv("JS_TOP_P", "0.5")
    cfg = from_env(cwd=tmp_path, save_session=False)
    assert cfg.settings["TEMPERATURE"] == 0.2

    turn = runtime.with_login_defaults(cfg, "b", None, None)

    assert turn.model_context_window == 131072
    assert turn.settings["TEMPERATURE"] == 0.9
    assert turn.settings["TOP_P"] == 0.5  # env stays


def test_a_base_url_override_takes_the_defaults_of_the_server_it_names(monkeypatch, tmp_path):
    from js import login_defaults

    _isolate_config(monkeypatch, tmp_path)
    logins.save_login(Login(provider_id="alpha", sdk_provider_id="openai", provider_base_url=BASE_URL,
                            provider_api_key="x", defaults={"CONTEXT_SIZE": 1000}))
    logins.save_login(Login(provider_id="beta", sdk_provider_id="openai", provider_base_url="http://beta:9/v1",
                            provider_api_key="x", defaults={"CONTEXT_SIZE": 2000}))

    assert login_defaults.login_for("alpha", "http://beta:9/v1").provider_id == "beta"
    assert login_defaults.login_for("alpha", BASE_URL).provider_id == "alpha"
    assert login_defaults.login_for("alpha", "http://unknown:1/v1") is None


def _two_model_login(**model_defaults) -> None:
    logins.save_login(Login(provider_id="testes", sdk_provider_id="openai", provider_base_url=BASE_URL,
                            provider_api_key="x", model_defaults=model_defaults))


def test_save_server_after_a_switch_writes_the_switched_models_values(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"CONTEXT_SIZE": 4096}, b={"CONTEXT_SIZE": 32768})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)

    run_async(monkeypatch, cfg, ["/save server"], model="testes/b")

    assert logins.load_logins_fresh()["testes"].model_defaults["b"] == {"CONTEXT_SIZE": 32768}


def test_a_model_without_defaults_falls_back_to_the_jsrc_values(monkeypatch, tmp_path):
    from js import runtime, turn_settings

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"CONTEXT_SIZE": 4096, "OUTPUT_TOKENS_MAX": 1000, "THINKING_BUDGET": 500})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    assert cfg.model_context_window == 4096

    turn = runtime.with_login_defaults(cfg, "b", None, None)

    defaults = turn_settings.project({})
    assert turn.model_context_window == defaults["model_context_window"]
    assert turn.max_output_tokens == defaults["max_output_tokens"]
    assert turn.thinking_budget == defaults["thinking_budget"]


def test_an_env_value_equal_to_a_default_still_beats_the_next_models_default(monkeypatch, tmp_path):
    from js import runtime

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"CONTEXT_SIZE": 4096}, b={"CONTEXT_SIZE": 32768})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    monkeypatch.setenv("JS_CONTEXT_SIZE", "4096")
    cfg = from_env(cwd=tmp_path, save_session=False)

    assert runtime.with_login_defaults(cfg, "b", None, None).model_context_window == 4096


@pytest.mark.parametrize("attr, key", cli.SETTING_FLAGS)
def test_a_setting_flag_beats_the_saved_login_defaults(monkeypatch, tmp_path, attr, key):
    from js import login_defaults, settings

    values = {"EFFORT": "max", "OUTPUT_TOKENS_MAX": 42, "TURN_ROUNDS_MAX": 7,
              "TEMPLATE_KWARGS": "enable_thinking=false", "DRY_SEQUENCE_BREAKERS": '["\\n", ":"]'}
    login_values = {"EFFORT": "low", "OUTPUT_TOKENS_MAX": 1000, "TEMPERATURE": 0.9, "TOP_K": 9,
                    "DRY_SEQUENCE_BREAKERS": ["x"]}
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a=login_values, b=login_values)
    monkeypatch.setenv("JS_MODEL", "testes/a")
    value = str(values.get(key, "0.5" if settings.SPEC_BY_KEY[key].type == "float" else 3))
    started = []

    def turn(cfg, system, messages, *_args, **_kwargs):
        started.append(cfg)
        messages.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli.runtime, "run_turn_async", turn_stub(turn))
    monkeypatch.chdir(tmp_path)
    assert js(monkeypatch, "--" + attr.replace("_", "-"), value, "-p", "hi") == 0

    [cfg] = started
    turn = login_defaults.for_turn(replace_model(cfg, "b"))

    if key in login_defaults.KEYS:
        assert login_defaults.sources_of(cfg)[key] == "flag"
    expected = setting_values.coerce_value(settings.SPEC_BY_KEY[key], value)[0]
    assert turn.settings[key] == expected


def replace_model(cfg, model):
    from dataclasses import replace

    return replace(cfg, model=model)


def _shown(monkeypatch, capsys, cfg, key, before=(), **state_kwargs):
    """What `/set KEY` prints in a REPL on ``state_kwargs`` after the lines
    ``before``, each typed once the turns before it have ended, and the turns
    the REPL started, the last one after `/set KEY`."""
    turns = record_turns(monkeypatch)
    capsys.readouterr()
    run_async(monkeypatch, cfg, [*before, f"/set {key}", "go"], wait=True, **state_kwargs)
    return [line for line in capsys.readouterr().out.splitlines() if key in line][-1], turns


def test_after_a_model_switch_set_and_save_see_the_values_the_turn_uses(monkeypatch, capsys, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"CONTEXT_SIZE": 4096}, b={"CONTEXT_SIZE": 32768})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)

    shown, turns = _shown(monkeypatch, capsys, cfg, "CONTEXT_SIZE", model="testes/b")

    assert turns[-1].cfg.model_context_window == 32768
    assert "32768" in shown
    assert "model" in shown


def test_save_leaves_a_models_login_defaults_out_of_jsrc(monkeypatch, tmp_path):
    from js import paths

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"CONTEXT_SIZE": 4096}, b={"CONTEXT_SIZE": 32768})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, ["go", "/save"], wait=True, model="testes/b")
    assert turns[0].cfg.model_context_window == 32768
    assert "32768" not in paths.global_config_file().read_text()

    monkeypatch.setenv("JS_MODEL", "testes/c")
    fresh = from_env(cwd=tmp_path, save_session=False)
    assert fresh.model == "c"
    assert fresh.model_context_window != 32768


def test_resolving_twice_after_a_switch_gives_the_same_sampling(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"TEMPERATURE": 0.2}, b={"TEMPERATURE": 0.9})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    state = ReplState(settings=dict(cfg.settings), model="testes/b")

    first = state.config_for_turn(cfg)
    second = state.config_for_turn(cfg)

    assert login_defaults.sampling(first).temperature == 0.9
    assert login_defaults.sampling(second).temperature == 0.9


def test_set_shows_the_agent_sampling_the_turn_sends(monkeypatch, capsys, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"TEMPERATURE": 0.2})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    agent = _agent_dir(tmp_path, "sampler", "sampling:\n  temperature: 0.7\ntools: []\n")
    cfg = replace(from_env(cwd=tmp_path, save_session=False), agent_id="sampler", prompts_dir=agent)

    shown, turns = _shown(monkeypatch, capsys, cfg, "TEMPERATURE")

    assert login_defaults.sampling(turns[-1].cfg).temperature == 0.7
    assert "0.7" in shown


def test_a_turn_on_another_model_sends_that_models_resolved_sampling(monkeypatch, tmp_path):
    from js import runtime

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"TEMPERATURE": 0.2}, b={"TEMPERATURE": 0.9})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    sent = {}

    async def fake_stream(**kwargs):
        sent.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", fake_stream)
    with pytest.raises(Exception):
        one_turn(cfg, "sys", [{"role": "user", "content": "hi"}], runtime.Telemetry(None),
                         model_override="b", suppress_output=True)
    assert sent["sampling"].temperature == 0.9


def test_after_switch_set_and_unset_the_shown_and_sent_sampling_agree(monkeypatch, capsys, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"TEMPERATURE": 0.2}, b={"TEMPERATURE": 0.9})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)

    shown, turns = _shown(monkeypatch, capsys, cfg, "TEMPERATURE",
                          ["/set TEMPERATURE 0.5", "set", "/set -TEMPERATURE"], model="testes/b")

    assert login_defaults.sampling(turns[0].cfg).temperature == 0.5
    assert login_defaults.sampling(turns[1].cfg).temperature == 0.9
    assert "0.9" in shown


def _agent_dir(tmp_path, name: str, yaml: str):
    d = tmp_path / ".js" / "agents" / name
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(yaml, encoding="utf-8")
    (d / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    return d


def test_a_swarm_agent_keeps_its_configured_effort_and_agent_cap(monkeypatch, tmp_path):
    import io
    import json

    from js import runtime, swarm, swarm_run

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(m={"EFFORT": "xhigh"})
    _agent_dir(tmp_path / "home", "seat", "max_tokens: 1777\ntools: []\n")
    monkeypatch.chdir(tmp_path)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"root": str(tmp_path / "bus"), "agents": [
        {"name": "one", "opener": "hi", "agent": "seat", "model": "testes/m", "effort": "high",
         "cwd": str(tmp_path)},
    ]}))
    started = []

    async def one_turn_then_stop(cfg, system, messages, *_args, **_kwargs):
        started.append(cfg)
        messages.append({"role": "assistant", "content": "done"})
        swarm.Room(tmp_path / "bus").send("steer", "*", "enough", kind="stop")

    monkeypatch.setattr(runtime, "run_turn_async", one_turn_then_stop)
    assert swarm_run.run(spec, out=io.StringIO()) == 0

    [cfg] = started
    turn = runtime.with_login_defaults(cfg, cfg.model, cfg.provider_id, cfg.provider_base_url)

    assert turn.reasoning_effort == "high"
    assert turn.max_output_tokens == 1777
    assert login_defaults.sources_of(turn)["EFFORT"] == "flag"


def test_a_model_switch_stamps_the_effort_the_new_model_resolves_to(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"EFFORT": "low"}, b={"EFFORT": "xhigh"})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    state = ReplState(settings=dict(cfg.settings), model="testes/b")

    assert repl_model_commands.live_stamp(cfg, state)["reasoning"] == "xhigh"


def test_a_resumed_session_keeps_its_saved_effort_on_its_model(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"EFFORT": "low"}, b={"EFFORT": "xhigh"})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_run_turn(cfg, system, messages, telemetry, **kwargs):
        seen["model"] = kwargs.get("model_override")
        seen["effort"] = kwargs.get("reasoning_effort_override", cfg.reasoning_effort)
        messages.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli.runtime, "run_turn_async", turn_stub(fake_run_turn))
    assert js(monkeypatch, "--session", "kept", "--model", "testes/b", "--reasoning", "low", "-p", "first") == 0

    assert js(monkeypatch, "--session", "kept", "-p", "again") == 0

    assert seen["model"] == "b"
    assert seen["effort"] == "low"


def test_only_explicit_set_lines_reach_the_set_layer(monkeypatch, tmp_path):
    import repl_driver

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"TEMPERATURE": 0.2}, b={"TEMPERATURE": 0.9})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    from dataclasses import replace

    # The test's own prompt directory: cfg's resolves to the bundled agent's.
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    cfg = replace(from_env(cwd=tmp_path, save_session=False), prompts_dir=prompts, prompt_roots=())
    live: dict = {}
    real_repl_state = repl_driver.repl_state

    def capturing_repl_state(*args, **kwargs):
        state, spec = real_repl_state(*args, **kwargs)
        live["state"] = state
        return state, spec

    monkeypatch.setattr(repl_driver, "repl_state", capturing_repl_state)
    sent: list = []

    def run_turn_stub(turn_cfg, *args, **kwargs):
        sent.append(kwargs["sampling"].temperature)
        if len(sent) == 1:
            # An `on turn_end set -TEMPERATURE` handler, run inside the turn.
            assert repl_settings_commands.cmd_set("-TEMPERATURE", live["state"], cfg) is None

    monkeypatch.setattr(cli.runtime, "run_turn_async", repl_driver.turn_stub(run_turn_stub))

    state = repl_driver.run_async(
        monkeypatch, cfg, ["/set TEMPERATURE 0.5", "one", "/model testes/b", "two", "exit"], wait=True,
    )

    assert "TEMPERATURE" not in state.live_set
    assert sent == [0.5, 0.9]


def test_an_explicit_set_equal_to_the_current_value_still_pins_it(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"TEMPERATURE": 0.2}, b={"TEMPERATURE": 0.9})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    state = ReplState(settings=dict(cfg.settings), messages=[], system="s")

    assert repl_settings_commands.cmd_set("TEMPERATURE 0.2", state, cfg) is None
    state.model = "testes/b"

    assert login_defaults.sampling(state.config_for_turn(cfg)).temperature == 0.2


def test_a_resumed_session_pins_its_effort_even_when_it_is_the_default(monkeypatch, tmp_path):
    from js import memory, run_assembly

    _isolate_config(monkeypatch, tmp_path)
    cfg = from_env(cwd=tmp_path, session="one")
    assert run_assembly.resume(cfg, None, None).layer == {}
    memory.persist_messages(cfg.session_file, [{"role": "user", "content": "hi"},
                                               {"role": "assistant", "content": "yo"}],
                            stamp=memory.stamp_for("b", "testes", "low"))

    assert run_assembly.resume(cfg, None, None).layer == {"EFFORT": "low"}
    assert run_assembly.resume(cfg, None, "high").layer == {}


def _delegate(parent, tmp_path, **options):
    """The task tool, called in a turn of ``parent``, giving one task to the
    worker agent."""
    from js.toolkit import ToolContext, subagents

    context = ToolContext(cwd=tmp_path)
    context.config = parent
    return subagents.task(tasks=["work"], agent_id="worker", context=context, **options)


def test_a_child_session_stamps_the_effort_its_turn_resolves(monkeypatch, tmp_path):
    from dataclasses import replace

    from js import agent_messages, memory, runtime

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    monkeypatch.setenv("JS_EFFORT", "low")
    root = tmp_path / "agents"
    for name, yaml in (("parent", "tools: []\n"), ("worker", "reasoning: high\ntools: []\n")):
        (root / name).mkdir(parents=True)
        (root / name / "agent.yaml").write_text(yaml, encoding="utf-8")
        (root / name / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    cfg = replace(from_env(cwd=tmp_path, save_session=False), prompts_dir=root / "parent",
                  prompt_roots=(root,), session_file=tmp_path / "parent.jsonl")
    seen = {}

    async def child_turn(child_cfg, system, messages, telemetry, **kwargs):
        seen["turn"] = child_cfg.reasoning_effort
        messages.append({"role": "assistant", "content": "done"})

    async def no_init(*args, **kwargs):
        return None

    monkeypatch.setattr(runtime, "run_turn_async", child_turn)
    monkeypatch.setattr(agent_messages, "initialize", no_init)
    monkeypatch.setattr(memory, "persist_messages",
                        lambda path, messages, stamp=None: seen.setdefault("stamp", stamp))

    result = _delegate(cfg, tmp_path)

    assert result == "done"
    assert seen["turn"] == "low"
    assert seen["stamp"]["reasoning"] == "low"


def test_an_out_of_range_agent_sampling_value_is_a_load_error_naming_it(monkeypatch, tmp_path):
    from dataclasses import replace

    from js import persona

    _isolate_config(monkeypatch, tmp_path)
    _save_custom_login("testes")
    monkeypatch.setenv("JS_MODEL", "testes/served")
    agent = _agent_dir(tmp_path, "worker", "sampling:\n  top_p: 2.0\n  temperature: 0.4\ntools: []\n")
    cfg = from_env(cwd=tmp_path, save_session=False)

    with pytest.raises(ValueError, match="top_p"):
        persona.load_configured_prompt_spec(replace(cfg, agent_id="worker", prompts_dir=agent), expand=False)


def test_a_child_whose_manifest_omits_reasoning_keeps_the_parent_agents(monkeypatch, tmp_path):
    from dataclasses import replace

    from js import agent_messages, memory, persona, runtime

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    root = tmp_path / "agents"
    for name, yaml in (("parent", "reasoning: high\ntools: []\n"), ("worker", "tools: []\n")):
        (root / name).mkdir(parents=True)
        (root / name / "agent.yaml").write_text(yaml, encoding="utf-8")
        (root / name / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    base = replace(from_env(cwd=tmp_path, save_session=False), prompts_dir=root / "parent",
                   prompt_roots=(root,), session_file=tmp_path / "parent.jsonl")
    parent_spec = persona.load_configured_prompt_spec(base, expand=False)
    cfg = login_defaults.for_turn(login_defaults.with_agent(base, parent_spec))
    seen = {}

    async def child_turn(child_cfg, system, messages, telemetry, **kwargs):
        seen["turn"] = child_cfg.reasoning_effort
        messages.append({"role": "assistant", "content": "done"})

    async def no_init(*args, **kwargs):
        return None

    monkeypatch.setattr(runtime, "run_turn_async", child_turn)
    monkeypatch.setattr(agent_messages, "initialize", no_init)
    monkeypatch.setattr(memory, "persist_messages", lambda *a, **k: None)

    _delegate(cfg, tmp_path)

    assert cfg.reasoning_effort == "high"
    assert seen["turn"] == "high"


@pytest.mark.parametrize(("worker", "expected"), [("tools: []\n", "low"), ("reasoning: medium\ntools: []\n", "medium")])
def test_a_child_takes_the_effort_its_parents_turn_ran_with(monkeypatch, tmp_path, worker, expected):
    from dataclasses import replace

    from js import agent_messages, memory, runtime

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"EFFORT": "high"})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    root = tmp_path / "agents"
    for name, yaml in (("parent", "tools: []\n"), ("worker", worker)):
        (root / name).mkdir(parents=True)
        (root / name / "agent.yaml").write_text(yaml, encoding="utf-8")
        (root / name / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    base = replace(from_env(cwd=tmp_path, save_session=False), prompts_dir=root / "parent",
                   prompt_roots=(root,), session_file=tmp_path / "parent.jsonl")
    # The parent's turn as the runtime hands it to delegation: a resumed
    # session's effort, sent as the request's override.
    parent = replace(login_defaults.for_turn(base), reasoning_effort="low")
    seen = {}

    async def child_turn(child_cfg, system, messages, telemetry, **kwargs):
        seen["turn"] = runtime.with_login_defaults(child_cfg, None, None, None).reasoning_effort
        messages.append({"role": "assistant", "content": "done"})

    async def no_init(*args, **kwargs):
        return None

    monkeypatch.setattr(runtime, "run_turn_async", child_turn)
    monkeypatch.setattr(agent_messages, "initialize", no_init)
    monkeypatch.setattr(memory, "persist_messages", lambda *a, **k: None)

    _delegate(parent, tmp_path)

    assert seen["turn"] == expected


@pytest.mark.parametrize(("worker", "expected"), [("tools: []\n", "low"), ("reasoning: medium\ntools: []\n", "medium")])
def test_a_parents_resumed_effort_sits_below_the_childs_agent_yaml(monkeypatch, tmp_path, worker, expected):
    from dataclasses import replace

    from js import agent_messages, memory, runtime

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"EFFORT": "high"})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    root = tmp_path / "agents"
    for name, yaml in (("parent", "tools: []\n"), ("worker", worker)):
        (root / name).mkdir(parents=True)
        (root / name / "agent.yaml").write_text(yaml, encoding="utf-8")
        (root / name / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    base = replace(from_env(cwd=tmp_path, save_session=False), prompts_dir=root / "parent",
                   prompt_roots=(root,), session_file=tmp_path / "parent.jsonl")
    parent = login_defaults.for_turn(replace(base, layer_resume={"EFFORT": "low"}))
    seen = {}

    async def child_turn(child_cfg, system, messages, telemetry, **kwargs):
        seen["turn"] = runtime.with_login_defaults(child_cfg, None, None, None).reasoning_effort
        messages.append({"role": "assistant", "content": "done"})

    async def no_init(*args, **kwargs):
        return None

    monkeypatch.setattr(runtime, "run_turn_async", child_turn)
    monkeypatch.setattr(agent_messages, "initialize", no_init)
    monkeypatch.setattr(memory, "persist_messages", lambda *a, **k: None)

    _delegate(parent, tmp_path)

    assert parent.reasoning_effort == "low"
    assert seen["turn"] == expected


def test_a_task_calls_reasoning_is_the_effort_the_childs_requests_send(monkeypatch, tmp_path):
    from dataclasses import replace

    from js import agent_messages, memory, runtime

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"EFFORT": "high"})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    root = tmp_path / "agents"
    for name in ("parent", "worker"):
        (root / name).mkdir(parents=True)
        (root / name / "agent.yaml").write_text("tools: []\n", encoding="utf-8")
        (root / name / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    parent = replace(from_env(cwd=tmp_path, save_session=False), prompts_dir=root / "parent",
                     prompt_roots=(root,), session_file=tmp_path / "parent.jsonl")
    seen = {}

    async def child_turn(child_cfg, system, messages, telemetry, **kwargs):
        resolved = runtime.with_login_defaults(child_cfg, None, None, None).reasoning_effort
        seen["sent"] = kwargs.get("reasoning_effort_override") or resolved
        messages.append({"role": "assistant", "content": "done"})

    async def no_init(*args, **kwargs):
        return None

    monkeypatch.setattr(runtime, "run_turn_async", child_turn)
    monkeypatch.setattr(agent_messages, "initialize", no_init)
    monkeypatch.setattr(memory, "persist_messages", lambda *a, **k: None)

    _delegate(parent, tmp_path, reasoning="low")

    assert seen["sent"] == "low"


def test_route_prefix_policy_and_login_lookup_are_injectable():
    looked_up = []
    def lookup(provider):
        looked_up.append(provider)
        return None
    route = resolve_model_route("openai/model", env={"OPENAI_API_KEY": "key"},
                                login_lookup=lookup, prefix_allowed=lambda *args: True)
    assert route.provider_id == "openai" and route.model == "model" and route.api_key == "key"
    assert looked_up and set(looked_up) == {"openai"}


def test_apply_route_puts_every_route_field_and_vision_on_the_config(monkeypatch, tmp_path):
    from js.config import apply_route
    from js.routing import ModelRoute

    _isolate_config(monkeypatch, tmp_path)
    cfg = from_env(save_session=False)
    route = ModelRoute(model="other-model", provider_id="testes", base_url=BASE_URL,
                       api_key="k", headers={"X-A": "1"})
    seeing = apply_route(replace(cfg, vision_enabled=False), route, settings={"VISION": True})
    assert (seeing.model, seeing.provider_id, seeing.provider_base_url, seeing.provider_api_key,
            seeing.provider_headers) == ("other-model", "testes", BASE_URL, "k", {"X-A": "1"})
    assert seeing.vision_enabled is True
