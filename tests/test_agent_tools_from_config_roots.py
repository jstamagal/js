"""Agents living in the operator's config dirs must become tools.

Regression for: `js -p "foo"` printing

    js: tool selector 'reviewer' matched no tool; ignoring

for every agent under ~/.config/js/agents. cli._registry_for returned a module-level
registry built with NO prompt roots, so only the js repo's own prompts/ dir was ever
scanned; cfg.prompt_roots reached the builder solely on the lock_subagent_model branch.

These tests go through cli._registry_for rather than calling build_default_registry with
an explicit path — a test that passes the path directly cannot catch this bug, because
the bug was that the path was never passed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from js import cli
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

    registry = cli._registry_for(_cfg((root,)))

    assert registry.resolve("reviewer") is not None
    assert "reviewer" in registry.select(["reviewer"]).by_name


def test_selector_for_a_config_agent_does_not_warn(tmp_path, capsys):
    root = tmp_path / "agents"
    _agent_dir(root, "triage")

    cli._registry_for(_cfg((root,))).select(["triage"])

    assert "matched no tool" not in capsys.readouterr().err


def test_locked_subagent_model_still_sees_config_agents(tmp_path):
    """The locked branch already passed prompt_roots; it must keep doing so."""
    root = tmp_path / "agents"
    _agent_dir(root, "coder")

    registry = cli._registry_for(_cfg((root,), lock=True))

    assert registry.resolve("coder") is not None
    assert "model" not in registry.resolve("task").params


def test_later_roots_shadow_earlier_ones(tmp_path):
    early, late = tmp_path / "early", tmp_path / "late"
    _agent_dir(early, "research")
    _agent_dir(late, "research")

    registry = cli._registry_for(_cfg((early, late)))

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
    assert "shadows a builtin tool name" in capsys.readouterr().err


def test_directory_without_markdown_is_not_an_agent(tmp_path):
    root = tmp_path / "agents"
    (root / "notanagent").mkdir(parents=True)
    (root / "notanagent" / "00-tools.yaml").write_text("tools: []\n", encoding="utf-8")

    assert registry_for_roots((root,)).resolve("notanagent") is None


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

    registry = cli._registry_for(_cfg((root,)))

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


def test_task_child_on_unreachable_agent_is_an_error_result_not_a_crash(tmp_path, monkeypatch):
    """The task worker's own dir lookup: ENODEV must become ValueError, which the
    worker returns as `ERROR could not load agent ...` to the calling agent."""
    import errno

    import pytest

    from js.toolkit.meta import _select_agent_prompt_dir

    root = tmp_path / "agents"
    dead = _agent_dir(root, "research")
    real_is_dir = Path.is_dir

    def is_dir(self, *args, **kwargs):
        if self == dead:
            raise OSError(errno.ENODEV, "No such device", str(self))
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)

    with pytest.raises(ValueError, match="research.*No such device"):
        _select_agent_prompt_dir("research", (root,))


def test_selecting_an_unreachable_agent_stops_with_a_plain_error(tmp_path, monkeypatch):
    """Skipping is only for the startup scan. `js -a asleep` must stop, cleanly."""
    import errno

    import pytest

    from js.config import _select_prompt_dir

    root = tmp_path / "agents"
    dead = _agent_dir(root, "asleep")
    real_is_dir = Path.is_dir

    def is_dir(self, *args, **kwargs):
        if self == dead:
            raise OSError(errno.ENODEV, "No such device", str(self))
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)

    with pytest.raises(ValueError, match="asleep.*No such device"):
        _select_prompt_dir("asleep", tmp_path / "repo", root, tmp_path / "project")
