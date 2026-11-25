"""Agents living in the operator's config dirs must become tools.

Regression for: `js -p "foo"` printing

    js: tool selector 'reviewer' matched no tool; ignoring

for every agent under ~/.config/js/agents. The run's registry was a module-level
registry built with NO prompt roots, so only the js repo's own prompts/ dir was ever
scanned; cfg.prompt_roots reached the builder solely on the lock_subagent_model branch.

These tests go through run_assembly.registry_for rather than calling build_default_registry with
an explicit path — a test that passes the path directly cannot catch this bug, because
the bug was that the path was never passed.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from js import run_assembly
from js.toolkit import ToolContext, subagents
from js.toolkit.registry import registry_for_roots


def _agent_dir(root: Path, name: str) -> Path:
    agent = root / name
    agent.mkdir(parents=True)
    (agent / "01-prompt.md").write_text("You are a test agent.\n", encoding="utf-8")
    return agent


def _cfg(roots: tuple[Path, ...], *, lock: bool = False) -> SimpleNamespace:
    return SimpleNamespace(prompt_roots=roots, lock_subagent_model=lock)


def test_agent_in_a_config_root_becomes_a_selectable_tool(tmp_path):
    root = tmp_path / "agents"
    _agent_dir(root, "reviewer")

    registry = run_assembly.registry_for(_cfg((root,)))

    assert registry.resolve("reviewer") is not None
    assert "reviewer" in registry.select(["reviewer:lazy"]).by_name


def _dispatched_agent(registry, tool_name, **args):
    async def worker(idx, total, item, context, cfg, tools, agent_id, *rest):
        return agent_id

    ctx = ToolContext()
    ctx.config = _cfg(())
    ctx.subagent_assembly = subagents.SubagentAssembly(
        registry_factory=lambda *a, **kw: registry, worker=worker)
    tool = registry.resolve(tool_name)
    return asyncio.run(subagents.dispatch_fan_out_async(tool, {"tasks": ["test"], **args}, ctx))


def test_nested_agent_tool_selects_and_dispatches_its_qualified_id(tmp_path):
    _agent_dir(tmp_path, "meta")
    _agent_dir(tmp_path, "meta/workflow-creator")
    registry = registry_for_roots((tmp_path,)).select(["meta:eager", "meta__workflow-creator:eager"])

    assert _dispatched_agent(registry, "meta") == "meta"
    child = registry.resolve("meta__workflow-creator")
    assert child.openai_spec()["function"]["name"] == "meta__workflow-creator"
    assert _dispatched_agent(registry, child.name) == "meta:workflow-creator"


def test_flat_agent_keeps_a_tool_name_shared_with_a_nested_agent(tmp_path, capsys):
    _agent_dir(tmp_path, "meta__worker")
    _agent_dir(tmp_path, "meta/worker")
    registry = registry_for_roots((tmp_path,))

    assert _dispatched_agent(registry, "meta__worker") == "meta__worker"
    assert _dispatched_agent(registry, "task", agent_id="meta:worker") == "meta:worker"
    assert "meta:worker" in capsys.readouterr().err


def test_ambiguous_nested_tool_name_is_available_only_via_task(tmp_path, capsys):
    _agent_dir(tmp_path, "one/two__three")
    _agent_dir(tmp_path, "one__two/three")
    registry = registry_for_roots((tmp_path,))

    assert registry.resolve("one__two__three") is None
    warning = capsys.readouterr().err
    for agent_id in ("one:two__three", "one__two:three"):
        assert agent_id in warning
        assert _dispatched_agent(registry, "task", agent_id=agent_id) == agent_id


def test_nested_tool_names_fit_the_provider_limit(tmp_path, capsys):
    fits = "w" * 58
    _agent_dir(tmp_path, f"meta/{fits}")
    _agent_dir(tmp_path, f"meta/{fits}x")
    registry = registry_for_roots((tmp_path,))

    assert registry.resolve(f"meta__{fits}") is not None
    assert registry.resolve(f"meta__{fits}x") is None
    assert f"meta:{fits}x" in capsys.readouterr().err


def test_selector_for_a_config_agent_does_not_warn(tmp_path, capsys):
    root = tmp_path / "agents"
    _agent_dir(root, "triage")

    run_assembly.registry_for(_cfg((root,))).select(["triage:lazy"])

    assert capsys.readouterr().err == ""


def test_locked_subagent_model_still_sees_config_agents(tmp_path):
    """The locked branch already passed prompt_roots; it must keep doing so."""
    root = tmp_path / "agents"
    _agent_dir(root, "coder")

    registry = run_assembly.registry_for(_cfg((root,), lock=True))

    assert registry.resolve("coder") is not None
    assert "model" not in registry.resolve("task").params


def test_later_roots_shadow_earlier_ones(tmp_path):
    early, late = tmp_path / "early", tmp_path / "late"
    _agent_dir(early, "research")
    _agent_dir(late, "research")

    registry = run_assembly.registry_for(_cfg((early, late)))

    assert len([t for t in registry.tools if t.name == "research"]) == 1


def test_cache_does_not_leak_between_different_roots(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    _agent_dir(one, "alpha_agent")
    _agent_dir(two, "beta_agent")

    first = registry_for_roots((one,))
    second = registry_for_roots((two,))

    assert first is not second
    assert first.resolve("alpha_agent") is not None
    assert first.resolve("beta_agent") is None
    assert second.resolve("beta_agent") is not None
    assert second.resolve("alpha_agent") is None


def test_cache_returns_the_same_registry_for_the_same_roots(tmp_path):
    root = tmp_path / "agents"
    _agent_dir(root, "stable_agent")

    assert registry_for_roots((root,)) is registry_for_roots((root,))


def test_flags_are_part_of_the_cache_key(tmp_path):
    root = tmp_path / "agents"
    _agent_dir(root, "flagged_agent")

    with_override = registry_for_roots((root,), flags=("model_override",))
    without = registry_for_roots((root,), flags=())

    assert with_override is not without
    assert "model" in with_override.resolve("task").params
    assert "model" not in without.resolve("task").params


def test_agent_shadowing_a_builtin_tool_name_warns(tmp_path, capsys):
    """A directory named after a builtin can never be selected; say so out loud."""
    root = tmp_path / "agents"
    _agent_dir(root, "read")

    registry = registry_for_roots((root,))

    assert registry.resolve("read").name == "read"
    warning = capsys.readouterr().err
    assert "read" in warning and str(root) in warning


def test_every_directory_in_a_root_is_an_agent(tmp_path):
    root = tmp_path / "agents"
    (root / "notesonly").mkdir(parents=True)
    (root / "notesonly" / "notes.txt").write_text("not a prompt\n", encoding="utf-8")
    (root / "manifestonly").mkdir(parents=True)
    (root / "manifestonly" / "agent.yaml").write_text("tools: []\n", encoding="utf-8")
    (root / "empty").mkdir(parents=True)
    (root / "plainfile").write_text("not a directory\n", encoding="utf-8")

    registry = registry_for_roots((root,))
    assert registry.resolve("notesonly") is not None
    assert registry.resolve("manifestonly") is not None
    assert registry.resolve("empty") is not None
    assert registry.resolve("plainfile") is None


def test_unreachable_agent_dir_is_skipped_not_fatal(tmp_path, monkeypatch, capsys):
    """A symlinked agent on a sleeping automount host stats as ENODEV.

    Seen live: ~/.config/js/agents/research -> ~/darkstar/... with darkstar's lid
    closed killed every `js -p` run with OSError before the model was called.
    """
    import errno

    root = tmp_path / "agents"
    _agent_dir(root, "alive")
    dead = _agent_dir(root, "asleep")

    real_is_dir = Path.is_dir

    def is_dir(self, *args, **kwargs):
        if self == dead:
            raise OSError(errno.ENODEV, "No such device", str(self))
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)

    registry = run_assembly.registry_for(_cfg((root,)))

    assert registry.resolve("alive") is not None
    assert registry.resolve("asleep") is None
    assert "asleep" in capsys.readouterr().err


def test_symlinked_agent_is_a_tool_without_following_the_link(tmp_path, monkeypatch, capsys):
    """Startup must not follow an agent symlink: following it wakes the host behind it.

    Seen live: sheape lists `research` (-> ~/darkstar/..., lid closed). Following
    the link stalled every run 5s and dropped `research` from her tools.
    """
    root = tmp_path / "agents"
    target = _agent_dir(tmp_path / "elsewhere", "research")
    root.mkdir()
    link = root / "research"
    link.symlink_to(target)

    real_is_dir, real_glob = Path.is_dir, Path.glob

    def is_dir(self, *args, **kwargs):
        assert self != link, f"followed {self}"
        return real_is_dir(self, *args, **kwargs)

    def glob(self, *args, **kwargs):
        assert self != link, f"globbed {self}"
        return real_glob(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(Path, "glob", glob)

    from js.toolkit.registry import _agent_tools

    names = [tool.name for tool in _agent_tools(root, set())]

    assert names == ["research"]
    assert capsys.readouterr().err == ""


def test_selecting_an_unreachable_agent_stops_with_a_plain_error(tmp_path, monkeypatch):
    """Skipping is only for the startup scan. `js -a asleep` and a task child
    on `asleep` must stop, cleanly: ENODEV becomes a ValueError, which the task
    worker returns as `ERROR could not load agent ...` to the calling agent."""
    import errno

    import pytest

    from js import paths

    root = tmp_path / "agents"
    dead = _agent_dir(root, "asleep")
    real_is_dir = Path.is_dir

    def is_dir(self, *args, **kwargs):
        if self == dead:
            raise OSError(errno.ENODEV, "No such device", str(self))
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)

    with pytest.raises(ValueError, match="asleep.*No such device"):
        paths.agent_dirs("asleep", (tmp_path / "repo", root, tmp_path / "project"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list an unreadable directory")
def test_an_unlistable_project_agent_does_not_fall_back(tmp_path):
    """A project agent whose directory cannot be listed must not become the
    global agent of the same name, which would run a different agent silently."""
    from js import paths

    repo_root = tmp_path / "repo" / "prompts"
    global_root = tmp_path / "global" / "agents"
    project_root = tmp_path / "project" / ".js" / "agents"
    _agent_dir(repo_root, "worker")
    _agent_dir(global_root, "worker")
    project_agent = _agent_dir(project_root, "worker")

    project_agent.chmod(0)
    try:
        with pytest.raises(ValueError, match="worker.*Permission denied"):
            paths.agent_dirs("worker", (repo_root, global_root, project_root))
    finally:
        project_agent.chmod(0o700)
