from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import ai

from js import cli, providers
from js.config import Config
from js.memory import append_message, load_messages

def make_cfg(tmp_path: Path) -> Config:
    return Config(
        agent_id="repl-agent",
        agent_dir=tmp_path / ".js" / "sessions" / "repl-agent",
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
        history_file=tmp_path / ".js" / "sessions" / "repl-agent" / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / "repl-agent",
        session_file=tmp_path / ".js" / "sessions" / "repl-agent" / "repl.jsonl",
        prompts_dir=tmp_path / "prompts" / "repl-agent",
    )


def test_reset_command_is_durable_for_later_session_load(tmp_path):
    cfg = make_cfg(tmp_path)
    append_message(cfg.session_file, {"role": "user", "content": "old"})
    state = {"messages": [{"role": "user", "content": "old"}]}

    handled = cli._handle_command("/reset", state, cfg)

    assert handled is True
    assert state["messages"] == []
    assert load_messages(cfg.session_file) == []


def test_refresh_model_catalog_command_forces_refresh(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    seen: list[str] = []

    def refresh_stub() -> bool:
        seen.append("forced")
        print("refreshed")
        return True

    monkeypatch.setattr(cli, "_force_refresh_model_catalog", refresh_stub)

    handled = cli._handle_command("/refresh-model-catalog", {"messages": []}, cfg)

    assert handled is True
    assert seen == ["forced"]
    assert "refreshed" in capsys.readouterr().out


def test_wipe_command_rotates_file_and_clears_in_process_messages(tmp_path):
    cfg = make_cfg(tmp_path)
    append_message(cfg.session_file, {"role": "user", "content": "old"})
    state = {"messages": [{"role": "user", "content": "old"}]}

    handled = cli._handle_command("/wipe", state, cfg)

    assert handled is True
    assert state["messages"] == []
    assert not cfg.session_file.exists()
    assert cfg.session_file.with_suffix(".jsonl.bak").exists()


def test_wipe_preserves_existing_backups_instead_of_overwriting(tmp_path):
    cfg = make_cfg(tmp_path)
    append_message(cfg.session_file, {"role": "user", "content": "first"})
    cli._handle_command("/wipe", {"messages": [{"role": "user", "content": "first"}]}, cfg)
    first_backup = cfg.session_file.with_suffix(".jsonl.bak")
    append_message(cfg.session_file, {"role": "user", "content": "second"})

    handled = cli._handle_command("/wipe", {"messages": [{"role": "user", "content": "second"}]}, cfg)

    assert handled is True
    assert first_backup.exists()
    assert cfg.session_file.with_suffix(".jsonl.bak.1").exists()
    assert load_messages(first_backup) == [{"role": "user", "content": "first"}]
    assert load_messages(cfg.session_file.with_suffix(".jsonl.bak.1")) == [{"role": "user", "content": "second"}]


def test_set_commands_parse_power_user_knobs(capsys):
    state = {"trace": False, "reasoning_effort": None, "max_output_tokens": None}

    assert cli._set_knob("/set debug on", state) is True
    assert cli._set_knob("/set reasoning max", state) is True
    assert cli._set_knob("/set maxout 64000", state) is True
    assert cli._set_knob("/set maxout auto", state) is True

    assert state == {"trace": True, "reasoning_effort": "high", "max_output_tokens": None}
    assert "maxout" in capsys.readouterr().out


def test_repl_runtime_exception_rolls_back_persisted_user_message(monkeypatch, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    append_message(cfg.session_file, {"role": "user", "content": "existing"})

    class SessionStub:
        def __init__(self, history=None):
            self.lines = iter(["cause failure", "exit"])

        def prompt(self, *args, **kwargs):
            line = next(self.lines)
            if line == "exit":
                return "exit"
            return line

    def run_turn_stub(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    captured = capsys.readouterr()
    assert actual == 0
    assert "RuntimeError: boom" in captured.out
    assert load_messages(cfg.session_file) == [{"role": "user", "content": "existing"}]



def test_provider_commands_mutate_runtime_state(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
    }

    assert cli._handle_command("/provider openai", state, cfg) is True
    assert cli._handle_command("/baseurl http://localhost:11434/v1", state, cfg) is True
    assert cli._handle_command("/apikey ollama", state, cfg) is True

    assert state["provider_id"] == "openai"
    assert state["provider_base_url"] == "http://localhost:11434/v1"
    assert state["provider_api_key"] == "ollama"


def test_first_class_provider_shortcuts_use_defaults(tmp_path, capsys, monkeypatch):
    # /provider <name> must resolve provider DEFAULTS — isolate from the operator's
    # real saved logins + provider env overrides, or a saved ollama login (e.g. a
    # Tailscale URL) leaks in and the test snaps on the real box.
    for var in (
        "OLLAMA_BASE_URL", "OLLAMA_LOCAL_BASE_URL", "OLLAMA_API_KEY", "OLLAMA_LOCAL_API_KEY",
        "LLAMACPP_BASE_URL", "LLAMA_CPP_BASE_URL", "LLAMACPP_API_KEY", "LLAMA_CPP_API_KEY",
        "XIAOMI_TP_BASE_URL", "MIMO_TP_BASE_URL", "XIAOMI_TP_KEY", "MIMO_TP_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli.logins, "load_logins", lambda *a, **k: {})

    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
    }

    assert cli._handle_command("/provider ollama", state, cfg) is True
    assert state["provider_id"] == "ollama"
    assert state["provider_base_url"] == providers.provider_base_url(providers.provider_for_login("ollama"), None)
    assert state["provider_api_key"] == providers.provider_api_key(providers.provider_for_login("ollama"), None)

    assert cli._handle_command("/provider llama.cpp", state, cfg) is True
    assert state["provider_id"] == "llama.cpp"
    assert state["provider_base_url"] == providers.provider_base_url(providers.provider_for_login("llama.cpp"), None)
    assert state["provider_api_key"] == providers.provider_api_key(providers.provider_for_login("llama.cpp"), None)

    assert cli._handle_command("/provider mimo-token-plan", state, cfg) is True
    assert state["provider_id"] == "mimo-token-plan"
    assert state["provider_base_url"] == providers.provider_base_url(providers.provider_for_login("mimo-token-plan"), None)
    assert state["provider_api_key"] == providers.provider_api_key(providers.provider_for_login("mimo-token-plan"), None)
    out = capsys.readouterr().out
    assert "ollama" in out
    assert "llama.cpp" in out
    assert "mimo-token-plan" in out


def test_login_loads_saved_provider_bundle(tmp_path):
    from js import logins

    cfg = make_cfg(tmp_path)
    logins.set_config_dir(tmp_path / "login-store")
    logins.save_login(
        logins.Login(
            provider_id="openai",
            sdk_provider_id="openai",
            provider_base_url="http://localhost:11434/v1",
            provider_api_key="ollama",
        )
    )
    state = {
        "model": cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
    }

    try:
        assert cli._handle_command("/login openai", state, cfg) is True
        assert state["provider_id"] == "openai"
        assert state["provider_base_url"] == "http://localhost:11434/v1"
        assert state["provider_api_key"] == "ollama"
    finally:
        logins.set_config_dir(None)


def test_logout_clears_provider_state(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": "openai",
        "provider_base_url": "http://localhost:11434/v1",
        "provider_api_key": "ollama",
    }

    assert cli._handle_command("/logout", state, cfg) is True

    assert state["provider_id"] is None
    assert state["provider_base_url"] is None
    assert state["provider_api_key"] is None


def test_models_command_lists_provider_models(monkeypatch, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": "openai",
        "provider_base_url": "http://localhost:11434/v1",
        "provider_api_key": "ollama",
    }

    async def fake_list_models():
        return ["model-a", "model-b", "model-c"]

    class FakeProvider:
        async def list_models(self):
            return await fake_list_models()

    monkeypatch.setattr(ai, "get_provider", lambda *args, **kwargs: FakeProvider())

    assert cli._handle_command("/models 2", state, cfg) is True

    out = capsys.readouterr().out
    assert "model-a" in out
    assert "model-b" in out
    assert "model-c" not in out
    assert "1 more" in out


def test_models_command_refuses_without_provider(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }

    assert cli._handle_command("/models", state, cfg) is True

    assert "no provider set" in capsys.readouterr().out


def test_provider_command_shows_current_value_when_bare(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "model": cfg.model,
        "provider_id": "openai",
        "provider_base_url": None,
        "provider_api_key": None,
    }

    assert cli._handle_command("/provider", state, cfg) is True
    assert "openai" in capsys.readouterr().out



def test_model_command_opens_picker_and_updates_state(monkeypatch, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "messages": [],
        "system": "SYSTEM",
        "model": cfg.model,
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }

    monkeypatch.setattr(
        cli.picker,
        "pick_model",
        lambda **kwargs: {
            "provider_id": "deepseek",
            "provider_base_url": None,
            "provider_api_key": "sk-test",
            "provider_headers": {},
            "model": "deepseek-v4-flash",
        },
    )

    assert cli._handle_command("/model", state, cfg) is True
    assert state["provider_id"] == "deepseek"
    assert state["provider_api_key"] == "sk-test"
    assert state["model"] == "deepseek-v4-flash"



def test_model_command_with_prefixed_provider_resets_provider_state(tmp_path):
    from js import logins

    cfg = replace(
        make_cfg(tmp_path),
        provider_id="deepseek",
        provider_base_url="https://api.deepseek.com",
        provider_api_key="sk-deepseek",
    )
    logins.set_config_dir(tmp_path / "login-store")
    logins.save_login(
        logins.Login(
            provider_id="ollama",
            provider_base_url="http://ollama.test/v1",
            provider_api_key="ollama",
        )
    )
    state = {
        "messages": [],
        "system": "SYSTEM",
        "model": cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
        "provider_headers": {"x-deepseek": "1"},
    }
    try:
        assert cli._handle_command("/model ollama/gemma4:e2b", state, cfg) is True
        assert state["provider_id"] == "ollama"
        assert state["provider_base_url"] == "http://ollama.test/v1"
        assert state["provider_api_key"] == "ollama"
        assert state["provider_headers"] == {}
        assert state["model"] == "gemma4:e2b"
    finally:
        logins.set_config_dir(None)

def test_pick_model_command_opens_picker_and_updates_state(monkeypatch, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {
        "messages": [],
        "system": "SYSTEM",
        "model": cfg.model,
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }

    monkeypatch.setattr(
        cli.picker,
        "pick_model",
        lambda **kwargs: {
            "provider_id": "openai",
            "provider_base_url": "http://proxy.test/v1",
            "provider_api_key": "sk-proxy",
            "provider_headers": {"x-proxy": "1"},
            "model": "proxy/model",
        },
    )

    assert cli._handle_command("/pick-model", state, cfg) is True
    assert state["provider_id"] == "openai"
    assert state["provider_base_url"] == "http://proxy.test/v1"
    assert state["provider_api_key"] == "sk-proxy"
    assert state["provider_headers"] == {"x-proxy": "1"}
    assert state["model"] == "proxy/model"
    assert "openai" in capsys.readouterr().out

def test_provider_command_uses_saved_login(tmp_path):
    from js import logins

    cfg = make_cfg(tmp_path)
    logins.set_config_dir(tmp_path / "login-store")
    logins.save_login(
        logins.Login(
            provider_id="my-proxy",
            sdk_provider_id="openai",
            provider_base_url="http://proxy.test/v1",
            provider_api_key="sk-proxy",
        )
    )
    state = {
        "model": cfg.model,
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }
    try:
        assert cli._handle_command("/provider my-proxy", state, cfg) is True
        assert state["provider_id"] == "my-proxy"
        assert state["provider_base_url"] == "http://proxy.test/v1"
        assert state["provider_api_key"] == "sk-proxy"
    finally:
        logins.set_config_dir(None)



def test_picker_open_preserves_explicit_logout_over_config(monkeypatch, tmp_path):
    cfg = replace(
        make_cfg(tmp_path),
        provider_id="openai",
        provider_base_url="http://cfg.test/v1",
        provider_api_key="cfg-key",
    )
    state = {
        "messages": [],
        "system": "SYSTEM",
        "model": cfg.model,
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }
    captured = {}

    def fake_pick_model(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(cli.picker, "pick_model", fake_pick_model)

    assert cli._handle_command("/model", state, cfg) is True
    assert captured["provider_id"] is None
    assert captured["provider_base_url"] is None
    assert captured["provider_api_key"] is None

def test_cfg_for_active_model_carries_provider_overrides(tmp_path):
    cfg = make_cfg(tmp_path)
    state = {
        "model": "other-model",
        "provider_id": None,
        "provider_base_url": "http://proxy.test/v1",
        "provider_api_key": "sk-proxy",
    }

    active = cli._cfg_for_active_model(cfg, state)
    assert active.model == "other-model"
    assert active.provider_id is None
    assert active.provider_base_url == "http://proxy.test/v1"
    assert active.provider_api_key == "sk-proxy"


def test_cfg_for_active_model_keeps_pinned_provider_for_prefixed_model(tmp_path):
    # A gateway/openrouter model id like "anthropic/claude-..." selected under an
    # explicitly pinned provider must NOT let the prefix hijack the provider or
    # inherit the gateway's base/api/headers onto a native provider transport.
    cfg = make_cfg(tmp_path)
    state = {
        "model": "anthropic/claude-sonnet-4",
        "provider_id": "omp",
        "provider_base_url": "https://gateway.test/v1",
        "provider_api_key": "sk-omp",
        "provider_headers": {"x-omp": "1"},
    }

    active = cli._cfg_for_active_model(cfg, state)
    assert active.provider_id == "omp"
    assert active.model == "anthropic/claude-sonnet-4"
    assert active.provider_base_url == "https://gateway.test/v1"
    assert active.provider_api_key == "sk-omp"
    assert active.provider_headers == {"x-omp": "1"}


def test_cfg_for_active_model_strips_same_provider_prefix(tmp_path):
    # When the prefix names the pinned provider, the prefix is stripped without
    # disturbing the pinned credentials.
    cfg = make_cfg(tmp_path)
    state = {
        "model": "deepseek/deepseek-v4-flash",
        "provider_id": "deepseek",
        "provider_base_url": "https://api.deepseek.com",
        "provider_api_key": "sk-deepseek",
    }

    active = cli._cfg_for_active_model(cfg, state)
    assert active.provider_id == "deepseek"
    assert active.model == "deepseek-v4-flash"
    assert active.provider_base_url == "https://api.deepseek.com"
    assert active.provider_api_key == "sk-deepseek"


def test_cfg_for_active_model_routes_prefix_when_provider_unset(tmp_path):
    # With no explicit provider, the model prefix still routes (AI-gateway case).
    cfg = make_cfg(tmp_path)
    state = {
        "model": "deepseek/deepseek-v4-flash",
        "provider_id": None,
        "provider_base_url": None,
        "provider_api_key": None,
    }

    active = cli._cfg_for_active_model(cfg, state)
    assert active.provider_id == "deepseek"
    assert active.model == "deepseek-v4-flash"