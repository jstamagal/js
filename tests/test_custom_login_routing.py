"""Custom `js --login` providers must be first-class routing citizens.

Regression for the FIXME cluster where a custom login (booty/testes/a local
llama.cpp box) was visible to /model and --list-models but invisible to
routing: `provider/model` prefixes shipped the WHOLE string to the default
provider, and pinning `PROVIDER` resolved base_url but handed the raw js id
to the SDK ("unknown provider id").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from js import cli, login_defaults, logins, messages as msgs, providers
from js.config import from_env
from js.logins import Login
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
        cli._resolve_cli_model_override(cfg, "huggingface/foo")

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
    actual = cli._resolve_cli_model_override(cfg, "anthropic/claude-x")

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
    actual = cli._resolve_cli_model_override(cfg, "testes/other")

    assert actual.provider_id == "testes"
    assert actual.model == "other"
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
    # cli._cfg_for_active_model passes use_saved_login=False; a builtin-provider
    # prefix that routes because of its login must still go to the login's
    # endpoint, not the provider default with an env key. ALL-CLAIMS.md:437.
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
    state = {"settings": dict(cfg.settings, TEMPERATURE=0.7, CONTEXT_SIZE=65536),
             "live_set": {"TEMPERATURE": 0.7, "CONTEXT_SIZE": 65536}}

    assert cli._cmd_save("server", state, cfg) is None

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

    turn = runtime._with_login_defaults(cfg, "b", None, None)

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

    assert cli._cmd_save("server", {"settings": dict(cfg.settings), "model": "testes/b"}, cfg) is None

    assert logins.load_logins_fresh()["testes"].model_defaults["b"] == {"CONTEXT_SIZE": 32768}


def test_a_model_without_defaults_falls_back_to_the_jsrc_values(monkeypatch, tmp_path):
    from js import runtime, turn_settings

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"CONTEXT_SIZE": 4096, "OUTPUT_TOKENS_MAX": 1000, "THINKING_BUDGET": 500})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    assert cfg.model_context_window == 4096

    turn = runtime._with_login_defaults(cfg, "b", None, None)

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

    assert runtime._with_login_defaults(cfg, "b", None, None).model_context_window == 4096


@pytest.mark.parametrize("attr, key", cli.SETTING_FLAGS)
def test_a_setting_flag_beats_the_saved_login_defaults(monkeypatch, tmp_path, attr, key):
    import argparse

    from js import login_defaults, settings

    values = {"EFFORT": "max", "OUTPUT_TOKENS_MAX": 42}
    login_values = {"EFFORT": "low", "OUTPUT_TOKENS_MAX": 1000}
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a=login_values, b=login_values)
    monkeypatch.setenv("JS_MODEL", "testes/a")
    args = argparse.Namespace(**{name: None for name, _key in cli.SETTING_FLAGS})
    setattr(args, attr, str(values[key]))
    extras, error = cli._setting_flag_extras(args)
    assert error is None

    cfg = from_env(cwd=tmp_path, save_session=False, extras=extras)
    turn = login_defaults.for_turn(replace_model(cfg, "b"))

    assert login_defaults.sources_of(cfg)[key] == "flag"
    expected = settings.coerce_value(settings.SPEC_BY_KEY[key], str(values[key]))[0]
    assert turn.settings[key] == expected


def replace_model(cfg, model):
    from dataclasses import replace

    return replace(cfg, model=model)


def test_after_a_model_switch_set_and_save_see_the_values_the_turn_uses(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"CONTEXT_SIZE": 4096}, b={"CONTEXT_SIZE": 32768})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    state = {"settings": dict(cfg.settings), "model": "testes/b"}

    turn = cli._cfg_for_live_state(cfg, state)
    shown = cli._live_settings_overlay_for_state(cfg, state)

    assert turn.model_context_window == 32768
    assert shown["CONTEXT_SIZE"].display == "32768"
    assert "model" in shown["CONTEXT_SIZE"].source


def test_save_leaves_a_models_login_defaults_out_of_jsrc(monkeypatch, tmp_path):
    from js import paths

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"CONTEXT_SIZE": 4096}, b={"CONTEXT_SIZE": 32768})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    state = {"settings": dict(cfg.settings), "model": "testes/b"}
    cli._cfg_for_live_state(cfg, state)

    assert cli._cmd_save("", state, cfg) is None
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
    state = {"settings": dict(cfg.settings), "model": "testes/b"}

    first = cli._cfg_for_live_state(cfg, state)
    second = cli._cfg_for_live_state(cfg, state)

    assert login_defaults.sampling(first).temperature == 0.9
    assert login_defaults.sampling(second).temperature == 0.9


def test_set_shows_the_agent_sampling_the_turn_sends(monkeypatch, tmp_path):
    from types import SimpleNamespace

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"TEMPERATURE": 0.2})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    spec = SimpleNamespace(sampling={"temperature": 0.7}, max_output_tokens=None, reasoning_effort=None)
    state = {"settings": dict(cfg.settings), "prompt_spec": spec}

    turn = cli._cfg_for_live_state(cfg, state)
    shown = cli._live_settings_overlay_for_state(cfg, state)

    assert login_defaults.sampling(turn).temperature == 0.7
    assert shown["TEMPERATURE"].display == "0.7"


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
        runtime.run_turn(cfg, "sys", [{"role": "user", "content": "hi"}], runtime.Telemetry(None),
                         model_override="b", suppress_output=True)
    assert sent["sampling"].temperature == 0.9


def test_after_switch_set_and_unset_the_shown_and_sent_sampling_agree(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"TEMPERATURE": 0.2}, b={"TEMPERATURE": 0.9})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    state = {"settings": dict(cfg.settings), "model": "testes/b", "messages": [], "system": "s"}

    assert cli._cmd_set("TEMPERATURE 0.5", state, cfg) is None
    assert login_defaults.sampling(cli._cfg_for_live_state(cfg, state)).temperature == 0.5
    assert cli._cmd_set("-TEMPERATURE", state, cfg) is None

    turn = cli._cfg_for_live_state(cfg, state)
    shown = cli._live_settings_overlay_for_state(cfg, state)
    assert login_defaults.sampling(turn).temperature == 0.9
    assert shown["TEMPERATURE"].display == "0.9"


def _agent_dir(tmp_path, name: str, yaml: str):
    d = tmp_path / ".js" / "agents" / name
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(yaml, encoding="utf-8")
    (d / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    return d


def test_a_swarm_agent_keeps_its_configured_effort_and_agent_cap(monkeypatch, tmp_path):
    from js import runtime, swarm_run

    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(m={"EFFORT": "xhigh"})
    _agent_dir(tmp_path / "home", "seat", "max_tokens: 1777\ntools: []\n")
    monkeypatch.chdir(tmp_path)
    spec = swarm_run.AgentSpec(name="one", opener="hi", agent="seat", model="testes/m", effort="high",
                               cwd=str(tmp_path))

    cfg, _system, _prompt_spec, _registry = swarm_run._configure(spec)
    turn = runtime._with_login_defaults(cfg, cfg.model, cfg.provider_id, cfg.provider_base_url)

    assert turn.reasoning_effort == "high"
    assert turn.max_output_tokens == 1777
    assert login_defaults.sources_of(turn)["EFFORT"] == "flag"


def test_a_model_switch_stamps_the_effort_the_new_model_resolves_to(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"EFFORT": "low"}, b={"EFFORT": "xhigh"})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    state = {"settings": dict(cfg.settings), "model": "testes/b"}

    assert cli._live_stamp(cfg, state)["reasoning"] == "xhigh"


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

    monkeypatch.setattr(cli.runtime, "run_turn", fake_run_turn)
    assert cli._run_prompt("first", session="kept", model="testes/b", reasoning="low") == 0

    assert cli._run_prompt("again", session="kept") == 0

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
            assert cli._cmd_set("-TEMPERATURE", live["state"], cfg) is None

    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)

    state = repl_driver.run_blocking(cfg, ["/set TEMPERATURE 0.5", "one", "/model testes/b", "two", "exit"])

    assert "TEMPERATURE" not in state["live_set"]
    assert sent == [0.5, 0.9]


def test_an_explicit_set_equal_to_the_current_value_still_pins_it(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _two_model_login(a={"TEMPERATURE": 0.2}, b={"TEMPERATURE": 0.9})
    monkeypatch.setenv("JS_MODEL", "testes/a")
    cfg = from_env(cwd=tmp_path, save_session=False)
    state = {"settings": dict(cfg.settings), "messages": [], "system": "s"}

    assert cli._cmd_set("TEMPERATURE 0.2", state, cfg) is None
    state["model"] = "testes/b"

    assert login_defaults.sampling(cli._cfg_for_live_state(cfg, state)).temperature == 0.2


def test_a_resumed_repl_session_pins_its_effort_even_when_it_is_the_default():
    stamp = {"model": "b", "provider": "testes", "reasoning": "low"}

    assert cli._resumed_layer(stamp, None) == {"EFFORT": "low"}
    assert cli._resumed_layer(stamp, "high") == {}
    assert cli._resumed_layer(None, None) == {}


def test_a_child_session_stamps_the_effort_its_turn_resolves(monkeypatch, tmp_path):
    import asyncio
    from dataclasses import replace

    from js import agent_messages, memory, runtime
    from js.toolkit import ToolContext, meta
    from js.toolkit.registry import build_default_registry

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

    result = asyncio.run(meta._run_one_task_async(
        0, 1, "work", ToolContext(cwd=tmp_path), cfg, build_default_registry(), "worker", None))

    assert result == "done"
    assert seen["turn"] == "low"
    assert seen["stamp"]["reasoning"] == "low"


def test_an_out_of_range_agent_sampling_value_is_refused_at_load_and_not_sent(monkeypatch, tmp_path, capsys):
    from dataclasses import replace

    from js import persona

    _isolate_config(monkeypatch, tmp_path)
    _save_custom_login("testes")
    monkeypatch.setenv("JS_MODEL", "testes/served")
    agent = _agent_dir(tmp_path, "worker", "sampling:\n  top_p: 2.0\n  temperature: 0.4\ntools: []\n")
    cfg = from_env(cwd=tmp_path, save_session=False)
    spec = persona.load_configured_prompt_spec(replace(cfg, agent_id="worker", prompts_dir=agent), expand=False)

    assert "top_p" in capsys.readouterr().err
    sent = login_defaults.sampling(cli._cfg_for_live_state(cfg, {"settings": dict(cfg.settings), "prompt_spec": spec}))

    assert sent.top_p is None
    assert sent.temperature == 0.4


def test_a_child_whose_manifest_omits_reasoning_keeps_the_parent_agents(monkeypatch, tmp_path):
    import asyncio
    from dataclasses import replace

    from js import agent_messages, memory, persona, runtime
    from js.toolkit import ToolContext, meta
    from js.toolkit.registry import build_default_registry

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

    asyncio.run(meta._run_one_task_async(
        0, 1, "work", ToolContext(cwd=tmp_path), cfg, build_default_registry(), "worker", None))

    assert cfg.reasoning_effort == "high"
    assert seen["turn"] == "high"


@pytest.mark.parametrize(("worker", "expected"), [("tools: []\n", "low"), ("reasoning: medium\ntools: []\n", "medium")])
def test_a_child_takes_the_effort_its_parents_turn_ran_with(monkeypatch, tmp_path, worker, expected):
    import asyncio
    from dataclasses import replace

    from js import agent_messages, memory, runtime
    from js.toolkit import ToolContext, meta
    from js.toolkit.registry import build_default_registry

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
        seen["turn"] = runtime._with_login_defaults(child_cfg, None, None, None).reasoning_effort
        messages.append({"role": "assistant", "content": "done"})

    async def no_init(*args, **kwargs):
        return None

    monkeypatch.setattr(runtime, "run_turn_async", child_turn)
    monkeypatch.setattr(agent_messages, "initialize", no_init)
    monkeypatch.setattr(memory, "persist_messages", lambda *a, **k: None)

    asyncio.run(meta._run_one_task_async(
        0, 1, "work", ToolContext(cwd=tmp_path), parent, build_default_registry(), "worker", None))

    assert seen["turn"] == expected


def test_route_prefix_policy_and_login_lookup_are_injectable():
    looked_up = []
    def lookup(provider):
        looked_up.append(provider)
        return None
    route = resolve_model_route("openai/model", env={"OPENAI_API_KEY": "key"},
                                login_lookup=lookup, prefix_allowed=lambda *args: True)
    assert route.provider_id == "openai" and route.model == "model" and route.api_key == "key"
    assert looked_up and set(looked_up) == {"openai"}
