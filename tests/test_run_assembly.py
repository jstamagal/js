"""A run settled against a temporary home: its agent, model, effort, system
prompt and tools, and what a resumed session hands it."""

from __future__ import annotations

import pytest

from js import logins, memory as M, run_assembly
from js.config import from_env
from js.logins import Login

BASE_URL = "http://localhost:8050/v1"


@pytest.fixture(autouse=True)
def testes_login(monkeypatch, tmp_path):
    monkeypatch.setattr(logins, "_CONFIG_DIR_OVERRIDE", tmp_path / "login-store")
    logins.save_login(Login(provider_id="testes", sdk_provider_id="openai",
                            provider_base_url=BASE_URL, provider_api_key="x"))
    monkeypatch.chdir(tmp_path)


def _agent(tmp_path, name: str, manifest: str, prompt: str = "SEAT PROMPT\n") -> None:
    directory = tmp_path / ".js" / "agents" / name
    directory.mkdir(parents=True)
    (directory / "agent.yaml").write_text(manifest, encoding="utf-8")
    (directory / "01-prompt.md").write_text(prompt, encoding="utf-8")


def _cfg(tmp_path, agent: str = "seat", **kwargs):
    return from_env(agent_id=agent, cwd=tmp_path, **kwargs)


def test_a_fresh_run_settles_on_the_agents_model_effort_cap_and_tools(tmp_path):
    _agent(tmp_path, "seat", "model: testes/a\nreasoning: high\nmax_tokens: 1777\ntools: [read:eager]\n")

    settled = run_assembly.settle(_cfg(tmp_path, save_session=False))

    assert (settled.config.provider_id, settled.config.model) == ("testes", "a")
    assert settled.config.provider_base_url == BASE_URL
    assert settled.config.reasoning_effort == "high"
    assert settled.config.max_output_tokens == 1777
    assert settled.system.startswith("SEAT PROMPT")
    assert "read" in settled.registry.by_name and "bash" not in settled.registry.by_name
    assert settled.effort is None


def test_a_named_model_and_effort_beat_the_agents(tmp_path):
    _agent(tmp_path, "seat", "model: testes/a\nreasoning: high\ntools: []\n")

    settled = run_assembly.settle(_cfg(tmp_path, save_session=False), model="testes/b", reasoning="low")

    assert (settled.config.provider_id, settled.config.model) == ("testes", "b")
    kwargs = settled.turn_kwargs(call_stats=[])
    assert kwargs["model_override"] == "b" and kwargs["provider_id_override"] == "testes"
    assert kwargs["reasoning_effort_override"] == "low"
    assert kwargs["tool_registry"] is settled.registry and kwargs["call_stats"] == []


def test_a_system_prompt_is_expanded(tmp_path):
    _agent(tmp_path, "seat", "tools: []\n", "on %%CURRENT_SESSION_MODEL%%\n")

    settled = run_assembly.settle(_cfg(tmp_path, save_session=False), model="testes/b")

    assert settled.system.strip() == "on b"
    assert settled.prompt_spec.source.strip() == "on %%CURRENT_SESSION_MODEL%%"


def test_a_resumed_session_settles_on_its_stamp_and_sends_its_recorded_prompt(tmp_path):
    _agent(tmp_path, "seat", "model: testes/a\ntools: []\n")
    cfg = _cfg(tmp_path, session="one")
    M.append_system_prompt(cfg.session_file, "RECORDED PROMPT")
    M.persist_messages(cfg.session_file, [{"role": "user", "content": "hi"},
                                          {"role": "assistant", "content": "yo"}],
                       stamp=M.stamp_for("b", "testes", "medium"))

    resumed = run_assembly.resume(cfg, None, None)
    settled = run_assembly.settle(cfg, resumed=resumed)

    assert resumed.from_stamp and resumed.model == "testes/b"
    assert resumed.layer == {"EFFORT": "medium"}
    assert (settled.config.provider_id, settled.config.model) == ("testes", "b")
    assert settled.config.reasoning_effort == "medium" and settled.effort == "medium"
    assert settled.recorded_system == "RECORDED PROMPT"
    assert settled.system == "RECORDED PROMPT"


def test_a_named_effort_replaces_the_stamps_and_a_named_model_its_model(tmp_path):
    _agent(tmp_path, "seat", "tools: []\n")
    cfg = _cfg(tmp_path, session="one")
    M.persist_messages(cfg.session_file, [{"role": "user", "content": "hi"},
                                          {"role": "assistant", "content": "yo"}],
                       stamp=M.stamp_for("b", "testes", "medium"))

    resumed = run_assembly.resume(cfg, "testes/a", "low")

    assert (resumed.model, resumed.from_stamp, resumed.layer) == ("testes/a", False, {})


def test_a_stamp_on_a_provider_without_a_login_is_reported_and_not_taken(tmp_path):
    _agent(tmp_path, "seat", "tools: []\n")
    cfg = _cfg(tmp_path, session="one")
    M.persist_messages(cfg.session_file, [{"role": "user", "content": "hi"},
                                          {"role": "assistant", "content": "yo"}],
                       stamp=M.stamp_for("m", "deepseek", None))

    resumed = run_assembly.resume(cfg, None, None)

    assert resumed.unreachable == "deepseek/m"
    assert resumed.model is None


def test_an_agent_that_does_not_load_and_a_model_that_does_not_route_are_not_settled(tmp_path):
    _agent(tmp_path, "seat", "tools: []\nbogus: 1\n")
    with pytest.raises(run_assembly.RunNotSettled) as failed:
        run_assembly.settle(_cfg(tmp_path, save_session=False))
    assert failed.value.loading

    _agent(tmp_path, "fine", "tools: []\n")
    with pytest.raises(run_assembly.RunNotSettled) as failed:
        run_assembly.settle(_cfg(tmp_path, "fine", save_session=False), model="deepseek/m")
    assert not failed.value.loading


@pytest.mark.parametrize("config_pins", [False, True])
def test_a_resumed_session_comes_back_on_its_stamp_not_agent_yaml(tmp_path, config_pins):
    from js import settings

    model_id = settings.default_value("MODEL")
    provider_id, _, _ = model_id.partition("/")
    logins.save_login(Login(provider_id=provider_id, provider_api_key="fixture-key"))
    _agent(tmp_path, "seat", "model: testes/a\ntools: []\n")
    cfg = _cfg(tmp_path, session="one")
    M.persist_messages(cfg.session_file, [{"role": "user", "content": "hi"},
                                          {"role": "assistant", "content": "yo"}],
                       stamp=M.stamp_for(cfg.model, cfg.provider_id, None))

    settled = run_assembly.settle(cfg, resumed=run_assembly.resume(cfg, None, None), config_pins=config_pins)

    assert (settled.config.provider_id, settled.config.model) == (cfg.provider_id, cfg.model)
