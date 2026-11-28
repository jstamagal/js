"""`js init`, `js init --full` and `js init agent NAME`."""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path

import pytest

from js import config, entry, init_cli, paths, persona, settings
from js.toolkit import policy, registry

_SKELETON = {".js/jsrc", *(f".js/{name}/.gitkeep" for name in init_cli.PROJECT_DIRS)}
_FULL = {"README.md", "LICENSE", ".gitignore"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An empty directory outside any repository, below a temporary home."""
    directory = tmp_path / "proj"
    directory.mkdir()
    monkeypatch.chdir(directory)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return directory


def _files(directory: Path) -> set[str]:
    return {str(path.relative_to(directory)) for path in directory.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(directory).parts[:1]}


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, check=False)


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {rel: (directory / rel).read_bytes() for rel in _files(directory)}


def test_init_makes_a_repository_and_the_skeleton(project):
    assert entry.main(["init"]) == 0
    assert (project / ".git").is_dir()
    assert _files(project) == _SKELETON
    assert (project / ".js/jsrc").read_bytes() == b""
    for name in init_cli.PROJECT_DIRS:
        assert (project / ".js" / name).is_dir()


def test_init_inside_a_repository_does_not_run_git_init(project, monkeypatch):
    _git(project, "init", "-q")
    head = (project / ".git/HEAD").read_bytes()
    sub = project / "sub"
    sub.mkdir()
    (project / ".git/js-marker").write_text("kept")
    monkeypatch.chdir(sub)
    assert entry.main(["init"]) == 0
    assert not (sub / ".git").exists()
    assert (project / ".git/js-marker").read_text() == "kept"
    assert (project / ".git/HEAD").read_bytes() == head
    assert _files(sub) == _SKELETON


def test_init_twice_changes_nothing(project):
    assert init_cli.run_init(full=False) == 0
    (project / ".js/jsrc").write_text("set MODEL mine/model\n")
    before = _snapshot(project)
    assert init_cli.run_init(full=False) == 0
    assert _snapshot(project) == before


def test_init_leaves_existing_parts_alone_and_fills_the_rest(project):
    (project / ".js/agents").mkdir(parents=True)
    (project / ".js/jsrc").write_text("set MODEL mine/model\n")
    assert init_cli.run_init(full=False) == 0
    assert (project / ".js/jsrc").read_text() == "set MODEL mine/model\n"
    # An existing directory gets nothing added to it.
    assert list((project / ".js/agents").iterdir()) == []
    for name in ("skills", "commands", "toolbox"):
        assert (project / ".js" / name / ".gitkeep").is_file()


def test_init_does_not_write_through_a_dangling_symlink(project, tmp_path):
    (project / ".js").mkdir()
    target = tmp_path / "elsewhere"
    (project / ".js/jsrc").symlink_to(target)
    assert init_cli.run_init(full=False) == 0
    assert not target.exists()
    assert (project / ".js/jsrc").is_symlink()


def test_init_refuses_where_dot_js_is_the_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    monkeypatch.chdir(tmp_path)
    assert (tmp_path / ".js").resolve() == paths.home().resolve()
    assert init_cli.run_init(full=True) != 0
    assert not (tmp_path / ".git").exists()
    assert not (tmp_path / "README.md").exists()


def test_an_empty_project_jsrc_is_an_empty_layer(project):
    paths.global_config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_file().write_text("set MODEL home/model\nset BANNER >>>\n")
    without = config.run_settings(project)
    assert init_cli.run_init(full=False) == 0
    assert project / ".js/jsrc" in config.jsrc_paths(project)
    within = config.run_settings(project)
    assert settings.knob(within, "MODEL") == "home/model"
    assert settings.knob(within, "BANNER") == ">>>"
    assert within == without


def test_the_skeleton_adds_no_agent_command_skill_or_tool(project):
    from js import prompt_commands, skills
    from js.toolkit import toolbox

    def seen():
        catalog = skills.discover_skills(project)
        return (paths.agent_names(), sorted(prompt_commands.discover(project)),
                sorted(toolbox.discover(project)), sorted(record.metadata.name for record in catalog._records.values()))

    before = seen()
    assert init_cli.run_init(full=False) == 0
    assert seen() == before


def test_init_full_adds_readme_license_gitignore(project):
    _git(project, "init", "-q")
    _git(project, "config", "user.name", "Ada Example")
    assert entry.main(["init", "--full"]) == 0
    assert _files(project) == _SKELETON | _FULL
    assert "proj" in (project / "README.md").read_text()
    license_text = (project / "LICENSE").read_text()
    assert "MIT" in license_text
    assert "Ada Example" in license_text
    assert str(datetime.date.today().year) in license_text


def test_init_full_license_falls_back_to_the_os_user(project, monkeypatch):
    monkeypatch.setattr(init_cli.getpass, "getuser", lambda: "osuser")
    assert init_cli.run_init(full=True) == 0
    assert "osuser" in (project / "LICENSE").read_text()


def test_init_full_never_overwrites(project):
    for name in _FULL:
        (project / name).write_text("mine\n")
    assert init_cli.run_init(full=True) == 0
    for name in _FULL:
        assert (project / name).read_text() == "mine\n"


def test_the_gitignore_ignores_secrets_and_local_settings_only(project):
    assert init_cli.run_init(full=True) == 0
    for rel in (".env", ".env.local", ".js/jsrc.local", "node_modules/x", "__pycache__/x.pyc", ".venv/x", "a.log"):
        assert _git(project, "check-ignore", "-q", rel).returncode == 0, rel
    for rel in (".env.example", ".js/jsrc", ".js/agents/.gitkeep", ".js/toolbox/.gitkeep", "README.md", "LICENSE"):
        assert _git(project, "check-ignore", "-q", rel).returncode == 1, rel


def test_init_agent_makes_the_agent_in_the_home(project):
    assert entry.main(["init", "agent", "dan"]) == 0
    agent = paths.global_agents_dir() / "dan"
    assert {path.name for path in agent.iterdir()} == {"agent.yaml", "01-prompt.md"}
    assert "dan" in paths.agent_names()
    spec = persona.load_agent_manifest(agent / "agent.yaml")
    stock = persona.load_agent_manifest(paths.defaults_dir() / "scaffold/agent.yaml")
    assert spec.tool_selectors == stock.tool_selectors
    assert "dan" in (agent / "01-prompt.md").read_text()


def test_init_agent_respects_the_home_flag(project, tmp_path):
    other = tmp_path / "other-home"
    assert entry.main(["--home", str(other), "init", "agent", "dan"]) == 0
    assert (other / "agents/dan/agent.yaml").is_file()


def test_init_agent_makes_a_nested_agent(project):
    assert init_cli.run_init_agent("meta:child") == 0
    assert (paths.global_agents_dir() / "meta/child/agent.yaml").is_file()
    assert "meta:child" in paths.agent_names()


def test_init_agent_refuses_an_existing_agent(project):
    agent = paths.global_agents_dir() / "dan"
    agent.mkdir(parents=True)
    (agent / "01-prompt.md").write_text("mine\n")
    assert entry.main(["init", "agent", "dan"]) != 0
    assert {path.name for path in agent.iterdir()} == {"01-prompt.md"}
    assert (agent / "01-prompt.md").read_text() == "mine\n"


@pytest.mark.parametrize("name", ["bad name", "../up", "a::b", ""])
def test_init_agent_refuses_an_invalid_name(project, name):
    before = sorted(paths.global_agents_dir().rglob("*")) if paths.global_agents_dir().exists() else []
    assert entry.main(["init", "agent", name]) != 0
    after = sorted(paths.global_agents_dir().rglob("*")) if paths.global_agents_dir().exists() else []
    assert after == before


def test_init_agent_proceeds_over_a_stock_name(project):
    assert init_cli.run_init_agent("slim") == 0
    assert (paths.global_agents_dir() / "slim/agent.yaml").is_file()
    assert (paths.defaults_dir() / "agents/slim/02-env.md").is_file()


def test_a_home_scaffold_template_replaces_the_stock_one(project):
    own = paths.home() / "scaffold/agent.yaml"
    own.parent.mkdir(parents=True)
    text = "# $$ and ${name} stay\ntools:\n  - read:eager\n"
    own.write_text(text)
    assert init_cli.run_init_agent("dan") == 0
    assert (paths.global_agents_dir() / "dan/agent.yaml").read_text() == text


def test_every_stock_agent_template_entry_selects_a_tool():
    spec = persona.load_agent_manifest(paths.defaults_dir() / "scaffold/agent.yaml")
    tools = registry.build_default_registry().tools
    for rule in policy.expand(spec.tool_selectors, policy.ToolsConfig(), "scaffold"):
        assert any(rule.matches(tool) for tool in tools), rule.entry.text


def test_init_refuses_json(project):
    with pytest.raises(SystemExit) as refused:
        entry.main(["init", "--json"])
    assert refused.value.code == 2
    assert not (project / ".js").exists()
