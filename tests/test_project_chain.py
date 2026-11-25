"""The project chain: the repository root found from `.git`, each `.js/` from
the root down to the working directory layered over the home, nearest wins."""
from __future__ import annotations

from pathlib import Path

import pytest

from js import paths, persona, prompt_commands, setting_layers, settings, skills
from js.config import InvocationSources, from_env, instruction_files_for, jsrc_paths, run_settings
from js.toolkit import toolbox


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _skill(root: Path, name: str, body: str) -> Path:
    return _write(root / name / "SKILL.md", f"---\nname: {name}\ndescription: {body}\n---\n{body}\n")


@pytest.fixture
def mono(tmp_path):
    """A repository with a nested package, both with their own `.js/`."""
    root = tmp_path / "mono"
    (root / ".git").mkdir(parents=True)
    package = root / "packages" / "pkg"
    package.mkdir(parents=True)
    return root, package


# -- root and chain ---------------------------------------------------------

def test_no_repository_means_no_project_layer(tmp_path):
    work = tmp_path / "loose"
    (work / ".js").mkdir(parents=True)
    assert paths.project_root(work) is None
    assert paths.project_chain(work) == ()
    assert paths.project_dirs(work) == ()


def test_chain_runs_from_the_root_down_to_the_working_directory(mono):
    root, package = mono
    for directory in (root, package):
        (directory / ".js").mkdir()
    assert paths.project_root(package) == root.resolve()
    assert paths.project_chain(package) == (root, root / "packages", package)
    assert paths.project_dirs(package) == (root / ".js", package / ".js")


def test_nothing_above_the_root_is_read(tmp_path, mono):
    root, package = mono
    (tmp_path / "outside" / ".js").mkdir(parents=True)
    (root.parent / ".js").mkdir(exist_ok=True)
    assert all(directory.is_relative_to(root) for directory in paths.project_dirs(package))


def test_a_submodule_lifts_to_its_superproject(mono):
    root, package = mono
    (root / ".git" / "modules" / "pkg").mkdir(parents=True)
    (package / ".git").write_text("gitdir: ../../.git/modules/pkg\n", encoding="utf-8")
    assert paths.project_root(package) == root.resolve()


def test_a_worktree_is_its_own_root(tmp_path, mono):
    root, _ = mono
    (root / ".git" / "worktrees" / "topic").mkdir(parents=True)
    tree = tmp_path / "trees" / "topic"
    tree.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {root / '.git' / 'worktrees' / 'topic'}\n", encoding="utf-8")
    assert paths.project_root(tree) == tree.resolve()


def test_home_as_the_root_loads_the_home_once(tmp_path):
    # The suite's HOME is tmp_path, so ~/.js is tmp_path/.js.
    (tmp_path / ".git").mkdir()
    (tmp_path / ".js").mkdir()
    work = tmp_path / "work"
    (work / ".js").mkdir(parents=True)
    assert paths.project_dirs(work) == (work / ".js",)
    assert jsrc_paths(work) == [paths.global_config_file(), work / ".js" / "jsrc", work / ".js" / "jsrc.local"]


# -- jsrc -------------------------------------------------------------------

def test_nearest_jsrc_wins_per_key(mono):
    root, package = mono
    _write(paths.global_config_file(), "set BANNER home\nset SPELLCHECK_TIMEOUT 7\n")
    _write(root / ".js" / "jsrc", "set BANNER root\nset NOTE_TIMESTAMP_FORMAT rootfmt\n")
    _write(package / ".js" / "jsrc", "set BANNER pkg\n")
    found = run_settings(package)
    assert settings.knob(found, "BANNER") == "pkg"
    assert settings.knob(found, "NOTE_TIMESTAMP_FORMAT") == "rootfmt"
    assert settings.knob(found, "SPELLCHECK_TIMEOUT") == 7


def test_jsrc_local_sits_over_its_own_layer_and_under_the_nearer_one(mono):
    root, package = mono
    _write(root / ".js" / "jsrc.local", "set BANNER root-local\n")
    _write(package / ".js" / "jsrc", "set NOTE_TIMESTAMP_FORMAT pkg\n")
    assert jsrc_paths(package) == [
        paths.global_config_file(),
        root / ".js" / "jsrc", root / ".js" / "jsrc.local",
        package / ".js" / "jsrc", package / ".js" / "jsrc.local",
    ]
    assert settings.knob(run_settings(package), "BANNER") == "root-local"


def test_unset_falls_back_to_the_layer_below(mono):
    root, package = mono
    _write(paths.global_config_file(), "set BANNER home\n")
    _write(root / ".js" / "jsrc", "set BANNER root\n")
    _write(package / ".js" / "jsrc", "set -BANNER\n")
    assert settings.knob(run_settings(package), "BANNER") == "root"
    assert settings.knob(run_settings(root), "BANNER") == "root"


def test_unset_in_the_home_jsrc_falls_back_to_stock(mono):
    _write(paths.global_config_file(), "set BANNER home\nset -BANNER\n")
    stock = settings.knob(settings.package_settings(), "BANNER")
    assert settings.knob(setting_layers.collect_settings([paths.global_config_file()], env={}), "BANNER") == stock


def test_presets_layer_through_the_chain(mono):
    root, package = mono
    _write(paths.global_config_file().with_name("jsrc.fast"), "set BANNER home-fast\n")
    _write(root / ".js" / "jsrc.fast", "set BANNER root-fast\n")
    (package / ".js").mkdir()
    found = run_settings(package, sources=InvocationSources(presets=("fast",)))
    assert settings.knob(found, "BANNER") == "root-fast"


# -- agents -----------------------------------------------------------------

def test_a_nearer_agent_replaces_the_lower_one_whole(mono):
    root, package = mono
    _write(root / ".js" / "agents" / "x" / "01-prompt.md", "ROOT SYSTEM\n")
    _write(root / ".js" / "agents" / "x" / "agent.yaml", "model: root-model\n")
    _write(package / ".js" / "agents" / "x" / "01-prompt.md", "PKG SYSTEM\n")
    roots = paths.agent_roots(package)
    assert paths.agent_dirs("x", roots) == (package / ".js" / "agents" / "x",)
    spec = persona.load_agent_prompt_spec("x", roots)
    assert spec.system == "PKG SYSTEM\n"
    assert spec.model == ""


def test_agents_from_every_layer_are_listed(mono):
    root, package = mono
    _write(root / ".js" / "agents" / "rooty" / "01-prompt.md", "R\n")
    _write(package / ".js" / "agents" / "pkgy" / "01-prompt.md", "P\n")
    names = paths.agent_names(paths.agent_roots(package))
    assert {"rooty", "pkgy", "defaultagent"} <= set(names)


def test_default_agent_resolves_through_the_chain(mono, monkeypatch):
    root, package = mono
    _write(root / ".js" / "jsrc", "set DEFAULT_AGENT coder\n")
    _write(package / ".js" / "agents" / "coder" / "01-prompt.md", "CODER\n")
    monkeypatch.chdir(package)
    cfg = from_env(save_session=False)
    assert cfg.agent_id == "coder"
    assert cfg.prompts_dir == (package / ".js" / "agents" / "coder",)


# -- instructions -----------------------------------------------------------

def test_instructions_prefer_js_md_and_fall_back_to_agents_md(mono):
    root, package = mono
    _write(root / "AGENTS.md", "root agents\n")
    _write(package / "JS.md", "pkg js\n")
    _write(package / "AGENTS.md", "pkg agents\n")
    _write(package / "AGENTS.local.md", "pkg local\n")
    found = instruction_files_for(package, ["JS.md", "JS.local.md"],
                                  [["JS.md", "AGENTS.md"], ["JS.local.md", "AGENTS.local.md"]])
    assert found == (root / "AGENTS.md", package / "JS.md", package / "AGENTS.local.md")


def test_instructions_are_read_from_each_js_dir(mono):
    root, package = mono
    home_md = _write(paths.home() / "JS.md", "home\n")
    package_md = _write(package / ".js" / "JS.md", "pkg setup\n")
    found = instruction_files_for(package, ["JS.md", "JS.local.md"], [["JS.md", "AGENTS.md"]])
    assert found == (home_md, package_md)


def test_no_repository_reads_only_the_home_instructions(tmp_path):
    work = tmp_path / "loose"
    _write(work / "AGENTS.md", "loose\n")
    home_md = _write(paths.home() / "JS.md", "home\n")
    assert instruction_files_for(work, ["JS.md"], [["JS.md", "AGENTS.md"]]) == (home_md,)


# -- skills, commands, toolbox, tools.yaml ----------------------------------

def test_nearer_skill_replaces_and_the_rest_are_inherited(mono, monkeypatch, tmp_path):
    root, package = mono
    monkeypatch.setattr(skills, "BUILTIN_SKILLS_DIR", tmp_path / "no-builtin")
    _skill(root / ".js" / "skills", "style", "root style")
    _skill(root / ".js" / "skills", "rooty", "root only")
    _skill(root / ".agents" / "skills", "shared", "root shared")
    _skill(package / ".js" / "skills", "style", "pkg style")
    catalog = skills.discover_skills(package)
    assert catalog.get("style").description == "pkg style"
    assert catalog.get("rooty") is not None
    assert catalog.get("shared") is not None


def test_commands_layer_through_the_chain(mono):
    root, package = mono
    _write(root / ".js" / "commands" / "hi.md", "root hi\n")
    _write(root / ".js" / "commands" / "only.md", "root only\n")
    _write(package / ".js" / "commands" / "hi.md", "pkg hi\n")
    found = prompt_commands.discover(package)
    assert found["hi"].path == package / ".js" / "commands" / "hi.md"
    assert "only" in found


def test_toolbox_layers_and_project_saves_go_to_the_nearest(mono):
    root, package = mono
    _write(root / ".js" / "toolbox" / "t.py", "def t():\n    return 1\n")
    (package / ".js").mkdir()
    assert toolbox.discover(package)["t"].scope == "project"
    assert toolbox.project_toolbox_dir(package) == package / ".js" / "toolbox"


def test_nearest_tools_yaml_wins(mono, monkeypatch):
    root, package = mono
    _write(root / ".js" / "tools.yaml", "tags: {}\n")
    nearest = _write(package / ".js" / "tools.yaml", "tags: {}\n")
    monkeypatch.chdir(package)
    assert paths.find("tools.yaml") == nearest


# -- TJ's monorepo ----------------------------------------------------------

def test_a_nested_package_brings_its_own_setup(mono, monkeypatch, tmp_path):
    """A package inside a monorepo with its own JS.md, defaultagent, coding
    agent and skills: run from inside the package, all of them take effect,
    and what the package does not replace comes from the repository root."""
    root, package = mono
    monkeypatch.setattr(skills, "BUILTIN_SKILLS_DIR", tmp_path / "no-builtin")
    setup = package / ".js"
    _write(setup / "JS.md", "PACKAGE RULES\n")
    _write(setup / "agents" / "defaultagent" / "01-prompt.md", "PACKAGE DEFAULT AGENT\n")
    _write(setup / "agents" / "coder" / "01-prompt.md", "PACKAGE CODER\n")
    _skill(setup / "skills", "pkg-build", "package build")
    _skill(root / ".js" / "skills", "repo-style", "repo style")

    monkeypatch.chdir(package)
    cfg = from_env(save_session=False)

    assert setup / "JS.md" in cfg.agents_files
    assert cfg.prompts_dir == (setup / "agents" / "defaultagent",)
    spec = persona.load_agent_prompt_spec(cfg.agent_id, cfg.prompt_roots, agents_files=cfg.agents_files)
    assert "PACKAGE RULES" in spec.system and "PACKAGE DEFAULT AGENT" in spec.system
    coder = persona.load_agent_prompt_spec("coder", cfg.prompt_roots)
    assert coder.system == "PACKAGE CODER\n"
    catalog = skills.discover_skills(package)
    assert catalog.get("pkg-build") is not None and catalog.get("repo-style") is not None
