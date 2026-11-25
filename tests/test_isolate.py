"""Isolation: a `.isolate` marker is a border in the setup layers. Nothing
above it is read, the home included; layers below it still stack. Stock stays
underneath."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from js import command_line, paths, prompt_commands, settings, skills
from js.config import InvocationSources, instruction_files_for, jsrc_paths, run_settings
from js.toolkit import toolbox

SETUP = ["JS.md", "JS.local.md"]
PROJECT = [["JS.md", "AGENTS.md"]]


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _skill(root: Path, name: str) -> Path:
    return _write(root / name / "SKILL.md", f"---\nname: {name}\ndescription: {name}\n---\n{name}\n")


@pytest.fixture
def mono(tmp_path):
    """A repository with a nested package, both with their own `.js/`."""
    root = tmp_path / "mono"
    (root / ".git").mkdir(parents=True)
    package = root / "packages" / "pkg"
    (root / ".js").mkdir()
    (package / ".js").mkdir(parents=True)
    return root, package


def test_no_marker_means_no_isolation(mono):
    _, package = mono
    assert paths.isolation(package) is None
    assert paths.uses_home(package)


@pytest.mark.parametrize("marker", [".isolate", ".js/.isolate"])
def test_a_marker_cuts_off_the_home_and_keeps_its_own_layer(mono, marker):
    root, _ = mono
    _write(root / marker)
    found = paths.isolation(root)
    assert found.directory == root and found.marker == root / marker
    assert not paths.uses_home(root)
    assert paths.project_dirs(root) == (root / ".js",)
    assert paths.home() not in paths.layers(root)
    assert paths.global_agents_dir() not in paths.agent_roots(root)


def test_layers_below_the_border_still_stack(mono):
    root, package = mono
    _write(root / ".isolate")
    assert paths.project_dirs(package) == (root / ".js", package / ".js")
    assert jsrc_paths(package) == [root / ".js" / "jsrc", root / ".js" / "jsrc.local",
                                   package / ".js" / "jsrc", package / ".js" / "jsrc.local"]


def test_a_border_cuts_off_the_layers_above_it(mono):
    root, package = mono
    _write(package / ".isolate")
    assert paths.project_chain(package) == (package,)
    assert paths.project_dirs(package) == (package / ".js",)


def test_the_nearest_marker_is_the_border(mono):
    root, package = mono
    _write(root / ".isolate")
    _write(package / ".js" / ".isolate")
    assert paths.isolation(package).directory == package
    assert paths.isolation(root).directory == root


def test_settings_under_a_border_ignore_the_home_and_keep_stock(mono):
    root, package = mono
    _write(paths.global_config_file(), "set BANNER home\nset SPELLCHECK_TIMEOUT 99\n")
    _write(root / ".js" / "jsrc", "set BANNER root\n")
    _write(root / ".isolate")
    found = run_settings(package)
    assert settings.knob(found, "BANNER") == "root"
    assert settings.knob(found, "SPELLCHECK_TIMEOUT") == settings.knob(settings.package_settings(),
                                                                       "SPELLCHECK_TIMEOUT")


def test_home_presets_are_cut_off_too(mono):
    root, package = mono
    _write(paths.global_config_file().with_name("jsrc.fast"), "set BANNER home-fast\n")
    _write(root / ".isolate")
    assert paths.global_config_file().with_name("jsrc.fast") not in jsrc_paths(
        package, InvocationSources(presets=("fast",)))


def test_instructions_skills_commands_and_toolbox_skip_the_home(mono, monkeypatch, tmp_path):
    root, package = mono
    monkeypatch.setattr(skills, "BUILTIN_SKILLS_DIR", tmp_path / "no-builtin")
    _write(paths.home() / "JS.md", "home\n")
    above = _write(root / "AGENTS.md", "root\n")
    own = _write(package / "AGENTS.md", "pkg\n")
    _skill(paths.global_skills_dir(), "homey")
    _skill(root / ".js" / "skills", "rooty")
    _skill(package / ".js" / "skills", "pkgy")
    _write(paths.global_commands_dir() / "homecmd.md", "x\n")
    _write(package / ".js" / "commands" / "pkgcmd.md", "x\n")
    _write(paths.global_toolbox_dir() / "ht.py", "def ht():\n    return 1\n")
    _write(package / ".isolate")

    assert instruction_files_for(package, SETUP, PROJECT) == (own,)
    assert above not in instruction_files_for(package, SETUP, PROJECT)
    catalog = skills.discover_skills(package)
    assert catalog.get("pkgy") is not None
    assert catalog.get("homey") is None and catalog.get("rooty") is None
    assert set(prompt_commands.discover(package)) >= {"pkgcmd"}
    assert "homecmd" not in prompt_commands.discover(package)
    assert paths.global_toolbox_dir() not in toolbox.toolbox_dirs(package)


def test_stock_agents_stay_underneath(mono):
    root, _ = mono
    _write(root / ".isolate")
    assert "defaultagent" in paths.agent_names(paths.agent_roots(root))


def test_tools_yaml_from_the_home_is_cut_off(mono, monkeypatch):
    root, package = mono
    _write(paths.tools_config_file(), "tags: {}\n")
    _write(root / ".isolate")
    monkeypatch.chdir(package)
    assert paths.find("tools.yaml") != paths.tools_config_file()


@pytest.mark.parametrize("where", ["home", "user"])
def test_a_home_marker_borders_nothing(mono, where):
    root, package = mono
    _write((paths.home() if where == "home" else paths.user_home()) / ".isolate")
    assert paths.isolation(package) is None
    assert paths.project_dirs(package) == (root / ".js", package / ".js")


def test_a_marker_beside_the_home_in_the_chain_borders_nothing(tmp_path):
    # The suite's HOME is tmp_path, so ~/.js is tmp_path/.js.
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".js" / ".isolate")
    work = tmp_path / "work"
    (work / ".js").mkdir(parents=True)
    assert paths.isolation(work) is None
    assert jsrc_paths(work)[0] == paths.global_config_file()


def test_no_isolate_ignores_every_marker(mono):
    root, package = mono
    _write(root / ".isolate")
    _write(paths.home() / ".isolate")
    paths.configure_isolate(False)
    assert paths.isolation(package) is None
    assert paths.project_dirs(package) == (root / ".js", package / ".js")


def test_isolate_borders_the_nearest_project_layer(mono):
    root, package = mono
    paths.configure_isolate(True)
    found = paths.isolation(package)
    assert found.directory == package and found.marker is None
    assert paths.project_dirs(package) == (package / ".js",)
    nested = package / "src"
    nested.mkdir()
    assert paths.isolation(nested).directory == package


def test_isolate_with_no_project_layer_borders_nothing(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    paths.configure_isolate(True)
    assert paths.isolation(loose) is None


def test_isolate_outside_a_repository_borders_the_nearest_js(tmp_path):
    scratch = tmp_path / "scratch"
    (scratch / ".js").mkdir(parents=True)
    (scratch / "sub").mkdir()
    paths.configure_isolate(True)
    assert paths.isolation(scratch / "sub").directory == scratch
    assert paths.project_dirs(scratch / "sub") == (scratch / ".js",)


# -- outside a repository ---------------------------------------------------

def test_a_marker_outside_a_repository_is_a_root(tmp_path):
    scratch = tmp_path / "scratch"
    _write(scratch / ".isolate")
    _write(scratch / ".js" / "jsrc", "set BANNER scratch\n")
    _write(paths.global_config_file(), "set BANNER home\n")
    work = scratch / "notes"
    (work / ".js").mkdir(parents=True)
    assert paths.project_root(work) == scratch
    assert paths.project_dirs(work) == (scratch / ".js", work / ".js")
    assert jsrc_paths(scratch)[0] == scratch / ".js" / "jsrc"
    assert settings.knob(run_settings(scratch), "BANNER") == "scratch"


def test_without_a_marker_a_loose_directory_has_no_project_layer(tmp_path):
    scratch = tmp_path / "scratch"
    (scratch / ".js").mkdir(parents=True)
    assert paths.project_root(scratch) is None and paths.project_dirs(scratch) == ()


# -- above the repository root ------------------------------------------------

def test_a_marker_above_the_root_cuts_the_home_but_reads_nothing_above_the_root(tmp_path):
    clients = tmp_path / "clients"
    _write(clients / ".isolate")
    _write(clients / ".js" / "jsrc", "set BANNER clients\n")
    _write(clients / "AGENTS.md", "clients\n")
    acme = clients / "acme"
    (acme / ".git").mkdir(parents=True)
    (acme / ".js").mkdir()
    found = paths.isolation(acme)
    assert found.directory == acme and found.marker == clients / ".isolate"
    assert not paths.uses_home(acme)
    assert paths.project_dirs(acme) == (acme / ".js",)
    assert instruction_files_for(acme, SETUP, PROJECT) == ()
    assert jsrc_paths(acme) == [acme / ".js" / "jsrc", acme / ".js" / "jsrc.local"]


# -- tracked markers ----------------------------------------------------------

def _git(cwd: Path, *words: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *words], check=True, capture_output=True)


def test_a_tracked_marker_is_ignored_and_reported(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    marker = _write(repo / ".isolate")
    (repo / ".js").mkdir()
    _git(repo, "add", ".isolate")
    assert paths.isolation(repo) is None
    assert paths.uses_home(repo)
    assert paths.tracked_markers(repo) == (marker,)


def test_an_untracked_marker_in_a_repository_is_honored(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo / ".isolate")
    assert paths.isolation(repo).directory == repo
    assert paths.tracked_markers(repo) == ()


def test_save_writes_the_border_layer_under_isolation(mono):
    root, package = mono
    assert paths.save_config_file(package) == paths.global_config_file()
    _write(root / ".isolate")
    assert paths.save_config_file(package) == root / ".js" / "jsrc"


def test_markers_are_never_created(mono):
    root, package = mono
    paths.configure_isolate(True)
    run_settings(package)
    assert not any(path.exists() for path in (root / ".isolate", package / ".isolate",
                                             package / ".js" / ".isolate", paths.home() / ".isolate"))


def test_both_flags_together_are_an_error():
    with pytest.raises(SystemExit) as raised:
        command_line.parser().parse_args(["--isolate", "--no-isolate"])
    assert raised.value.code == 2


@pytest.mark.parametrize("override, words", [(None, []), (True, ["--isolate"]), (False, ["--no-isolate"])])
def test_the_override_is_carried_to_a_rerun(override, words):
    paths.configure_isolate(override)
    assert command_line.argv(*command_line.isolate_argv()) == words


def test_the_repl_banner_names_the_border(mono, monkeypatch):
    from js import repl
    root, package = mono
    monkeypatch.chdir(package)
    assert repl._isolation_lines() == []
    _write(root / ".isolate")
    assert len(repl._isolation_lines()) == 1
