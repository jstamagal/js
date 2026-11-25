"""Which layer directories and files each agent is assembled from, seen through
the public entries: `config.from_env` with `persona.load_configured_prompt_spec`,
`--list-agents`, `--verify` and the tool registry a run selects from."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from js import cli, config, paths, persona
from js.cli_options import RunOptions, invocation
from js.toolkit.registry import registry_for_roots

STOCK, HOME, PROJECT = "stock", "home", "project"

# agent -> layer -> {filename: text}; an empty mapping is an empty directory.
AGENTS = {
    "prompt-only": {STOCK: {"01-prompt.md": "stock prompt"}, HOME: {"01-prompt.md": "home prompt"}},
    "yaml-only": {STOCK: {"01-prompt.md": "stock prompt", "agent.yaml": "model: stock-model\n"},
                  PROJECT: {"agent.yaml": "model: project-model\n"}},
    "both": {STOCK: {"01-prompt.md": "stock prompt", "agent.yaml": "model: stock-model\n"},
             HOME: {"01-prompt.md": "home prompt", "agent.yaml": "model: home-model\n"}},
    "shadowed": {STOCK: {"01-prompt.md": "stock prompt", "02-env.md": "stock env"}, HOME: {"02-env.md": ""}},
    "manifest": {HOME: {"agent.yaml": "model: home-model\n"}},
    "one-layer": {PROJECT: {"01-prompt.md": "project prompt"}},
    "empty": {HOME: {}},
    "notes": {STOCK: {"01-prompt.md": "stock prompt"}, PROJECT: {"notes.txt": "not an agent file"}},
}

# agent -> (layers in prompts_dir, assembled system prompt, agent.yaml model)
ASSEMBLED = {
    "prompt-only": ((STOCK, HOME), "home prompt\n", ""),
    "yaml-only": ((STOCK, PROJECT), "stock prompt\n", "project-model"),
    "both": ((STOCK, HOME), "home prompt\n", "home-model"),
    "shadowed": ((STOCK, HOME), "stock prompt\n", ""),
    "manifest": ((HOME,), "\n", "home-model"),
    "one-layer": ((PROJECT,), "project prompt\n", ""),
    "empty": ((HOME,), "\n", ""),
    "notes": ((STOCK, PROJECT), "stock prompt\n", ""),
}


@pytest.fixture
def layers(tmp_path, monkeypatch):
    """A stock layer mirroring defaults/ with the fixture agents added, the
    home layer, and a project directory js runs in."""
    real = paths.defaults_dir()
    stock = tmp_path / "stock"
    (stock / "agents").mkdir(parents=True)
    for entry in real.iterdir():
        if entry.name != "agents":
            (stock / entry.name).symlink_to(entry)
    for entry in (real / "agents").iterdir():
        (stock / "agents" / entry.name).symlink_to(entry)
    project = tmp_path / "project"
    roots = {STOCK: stock / "agents", HOME: paths.home() / "agents", PROJECT: project / ".js" / "agents"}
    for agent, by_layer in AGENTS.items():
        for layer, files in by_layer.items():
            directory = roots[layer] / agent
            directory.mkdir(parents=True)
            for name, text in files.items():
                (directory / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(paths, "defaults_dir", lambda: stock)
    monkeypatch.chdir(project)
    return roots


def _layer(roots, directory):
    return next(layer for layer, root in roots.items() if directory.parent == root)


@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_agent_is_assembled_from_its_layers(layers, agent):
    cfg = config.from_env(agent_id=agent, cwd=Path.cwd(), save_session=False)
    spec = persona.load_configured_prompt_spec(cfg, expand=False)
    expected_layers, system, model = ASSEMBLED[agent]
    assert tuple(_layer(layers, directory) for directory in cfg.prompts_dir) == expected_layers
    assert (spec.system, spec.model) == (system, model)


def test_chat_takes_the_agent_prompt_from_its_layers_without_env(layers):
    with invocation(RunOptions(chat=True, agent="shadowed")):
        cfg = config.from_env(agent_id="shadowed", cwd=Path.cwd(), save_session=False)
        assert persona.load_configured_prompt_spec(cfg, expand=False).system == "stock prompt\n"
    (layers[HOME] / "shadowed" / "02-env.md").unlink()
    cfg = config.from_env(agent_id="shadowed", cwd=Path.cwd(), save_session=False)
    assert persona.load_configured_prompt_spec(cfg, expand=False).system == "stock prompt\n\nstock env\n"
    with invocation(RunOptions(chat=True, agent="shadowed")):
        assert persona.load_configured_prompt_spec(cfg, expand=False).system == "stock prompt\n"


def test_missing_agent_names_every_directory_searched(layers):
    cfg = config.from_env(agent_id="missing", cwd=Path.cwd(), save_session=False)
    searched = tuple(root / "missing" for root in layers.values())
    assert cfg.prompts_dir == searched
    with pytest.raises(FileNotFoundError) as error:
        persona.load_configured_prompt_spec(cfg, expand=False)
    assert "missing" in str(error.value)
    assert all(str(directory) in str(error.value) for directory in searched)


def test_list_agents_is_every_agent_directory_of_every_layer(layers, capsys):
    assert cli.main(["--list-agents", "--json"]) == 0
    stock = {entry.name for entry in layers[STOCK].iterdir()}
    assert json.loads(capsys.readouterr().out) == sorted(stock | set(AGENTS))


def test_verify_reads_every_layer_of_every_agent(layers, capsys):
    (layers[PROJECT] / "broken").mkdir()
    (layers[PROJECT] / "broken" / "agent.yaml").write_text("model: [\n", encoding="utf-8")
    cli.main(["--verify", "--json"])
    errors = json.loads(capsys.readouterr().out)["errors"]
    assert not [error for error in errors if any(f"/{agent}" in error for agent in AGENTS)]
    assert [error for error in errors if str(layers[PROJECT] / "broken") in error]


def test_verify_of_a_missing_agent_names_every_directory_searched(layers, capsys):
    assert cli.main(["--verify", "missing", "--json"]) == 1
    errors = json.loads(capsys.readouterr().out)["errors"]
    assert len(errors) == 1
    assert all(str(root / "missing") in errors[0] for root in layers.values())


def test_tool_registry_offers_every_agent_directory(layers):
    cfg = config.from_env(cwd=Path.cwd(), save_session=False)
    offered = {tool.name for tool in registry_for_roots(cfg.prompt_roots).tools if tool.source == "agent"}
    assert offered >= set(AGENTS)


def test_a_lone_agent_directory_without_prompt_or_manifest_is_an_agent(tmp_path):
    directory = tmp_path / "bare"
    directory.mkdir()
    (directory / "notes.txt").write_text("not an agent file", encoding="utf-8")
    cfg = config.Config(model="m", agent_id="bare", prompts_dir=directory)
    spec = persona.load_configured_prompt_spec(cfg, expand=False)
    assert (spec.system, spec.tool_selectors) == ("\n", ())
