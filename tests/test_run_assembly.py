"""A run settled against a temporary home: its agent, model, effort, system
prompt and tools, and what a resumed session hands it."""

from __future__ import annotations

import pytest

from js import logins, memory as M, run_assembly, tool_calls
from js.cli_options import RunOptions, invocation
from js.config import from_env
from js.logins import Login
from js.toolkit import policy
from js.toolkit.registry import stock_registry

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


@pytest.mark.parametrize(("options", "eager", "lazy"), [
    (RunOptions(), {"read", "shell", "write", "patch", "undo", "fs_search"}, {"lsp"}),
    (RunOptions(pi=True), {"read", "bash", "write", "edit", "fs_search", "undo"}, {"lsp"}),
    (RunOptions(pi=True, tools="fs_search"), {"read", "bash", "write", "edit", "fs_search"}, set()),
    (RunOptions(pi_strict=True), {"read", "bash", "write", "edit"}, set()),
    (RunOptions(pi=True, no_tools=True), set(), set()),
    (RunOptions(pi_strict=True, chat=True), set(), set()),
])
def test_pi_tool_modes_select_the_model_surface(tmp_path, options, eager, lazy):
    _agent(tmp_path, "seat", 'tools: [read:eager, shell:eager, write:eager, patch:eager, '
                           'undo:eager, fs_search:eager, lsp:lazy]\n')
    with invocation(options):
        settled = run_assembly.settle(_cfg(tmp_path, save_session=False))
        surface = settled.registry.lazy_surface(tmp_path)
        if lazy:
            eager = eager | {"tool_discovery"}
        specs = tool_calls.aliased_tool_specs(surface.openai_specs(), settled.registry.public_aliases)
        assert {spec["function"]["name"] for spec in specs} == eager
        assert settled.registry.lazy == lazy
        if options.pi_strict:
            assert options.skips_mcp


def test_stock_surface_keeps_native_tools_and_wildcard_selects_pi(tmp_path):
    pi_names = {"pi_read", "pi_bash", "pi_edit", "pi_write"}
    assert pi_names.isdisjoint(stock_registry().by_name)

    _agent(tmp_path, "seat", 'tools: ["*:eager"]\n')
    settled = run_assembly.settle(_cfg(tmp_path, save_session=False))
    assert pi_names <= settled.registry.by_name.keys()


def test_mixed_pi_keeps_aliases_for_unreplaced_tools(tmp_path):
    _agent(tmp_path, "seat", "tools: [fs_search:eager, read:eager]\n"
                           "aliases: {fs_search: locate, read: old_read}\n")
    with invocation(RunOptions(pi=True)):
        registry = run_assembly.settle(_cfg(tmp_path, save_session=False)).registry
        assert registry.resolve("locate").name == "fs_search"
        assert registry.resolve("read").name == "pi_read"
        assert registry.resolve("old_read") is None


def test_pi_bash_inherits_native_shell_argument_bans():
    error = policy.argument_refusal("pi_bash", {"command": "rm -rf /"}, {"shell": ["rm -rf"]})
    assert error


def test_manifest_aliases_follow_lazy_tools_into_dispatch(tmp_path):
    _agent(tmp_path, "seat", "tools: [pi_edit:lazy]\n"
                            "aliases: {pi_edit: edit, tool_discovery: find_tool}\n")
    registry = run_assembly.settle(_cfg(tmp_path, save_session=False)).registry
    surface = registry.lazy_surface(tmp_path)
    assert surface.dispatch_registry().resolve("find_tool").name == "tool_discovery"
    entry, = surface.catalog()
    assert entry.name == "edit"
    surface.discover(load=entry.id)
    assert surface.dispatch_registry().resolve("edit").name == "pi_edit"
    specs = tool_calls.aliased_tool_specs(surface.openai_specs(), registry.public_aliases)
    assert {spec["function"]["name"] for spec in specs} == {"edit", "find_tool"}


def test_manifest_aliases_reject_duplicate_public_names(tmp_path):
    _agent(tmp_path, "seat", "tools: [read:eager, fs_search:lazy]\n"
                            "aliases: {read: lookup, fs_search: LOOKUP}\n")
    with pytest.raises(run_assembly.RunNotSettled):
        run_assembly.settle(_cfg(tmp_path, save_session=False))
