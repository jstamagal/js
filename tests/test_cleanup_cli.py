"""`js cleanup [DIR ...]`: the rules over each workspace, then the stock
cleanup agent only where something needs reading, kept in the workspace with
its archive area and the one log the only other places it can write."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from jail_probe import needs_bwrap

from js import (
    cleanup,
    cleanup_cli,
    cli,
    jail,
    messages as msgs,
    paths,
    persona,
    prompt_run,
    run_assembly,
    turn_settings,
)
from js.toolkit import ToolContext, call_tool, shell
from js.toolkit.registry import build_default_registry

DAY = 86400


@pytest.fixture(autouse=True)
def forty_days_on(monkeypatch):
    """Runs happen 40 days after the files the tests make."""
    later = time.time() + 40 * DAY
    monkeypatch.setattr(cleanup, "_now", lambda: later)


def bench_piece(workspace: Path) -> Path:
    piece = workspace / "bench"
    piece.mkdir(parents=True)
    (piece / "results.csv").write_text("tok/s\n41\n")
    return piece


@pytest.fixture
def agent_runs(monkeypatch):
    """Each command line the cleanup agent was started with."""
    runs: list[list[str]] = []

    monkeypatch.setattr(cleanup_cli, "_run_agent", lambda argv: runs.append(argv) or 0)
    return runs


def test_the_cleanup_agent_loads_with_its_tool_surface():
    spec = persona.load_prompt_spec(paths.agent_dirs("cleanup"))
    registry = build_default_registry().select(spec.tool_selectors)

    assert set(registry.by_name) == {"read", "fs_search", "shell", "write"}
    assert not registry.lazy
    assert spec.system.strip()


def test_cleanup_with_no_directory_cleans_the_home_work_directory(monkeypatch):
    seen = []
    monkeypatch.setattr(cleanup.Cleanup, "run", lambda self: seen.append(self.workspace) or self.report)
    paths.work_dir().mkdir(parents=True, exist_ok=True)

    assert cli.main(["cleanup", "--no-model"]) == 0

    assert seen == [paths.work_dir().resolve()]


def test_cleanup_dirs_names_the_workspaces(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(cleanup.Cleanup, "run", lambda self: seen.append(self.workspace) or self.report)
    (tmp_path / "agents" / "claude").mkdir(parents=True)
    (tmp_path / "agents" / "codex").mkdir()
    monkeypatch.setenv("JS_CLEANUP_DIRS", f'["{tmp_path}/agents/*"]')

    assert cli.main(["cleanup", "--no-model"]) == 0

    assert seen == [tmp_path / "agents" / "claude", tmp_path / "agents" / "codex"]


def test_a_missing_directory_exits_two(monkeypatch, tmp_path):
    warned = []
    monkeypatch.setattr(msgs, "warn", lambda message, **fields: warned.append(message))

    assert cli.main(["cleanup", str(tmp_path / "missing"), "--no-model"]) == 2

    assert warned == [msgs.CLEANUP_NOT_A_DIRECTORY]


def test_a_run_with_nothing_to_read_calls_no_model(agent_runs, tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "_now", time.time)
    workspace = tmp_path / "work"
    (workspace / "young").mkdir(parents=True)
    (workspace / "young" / "notes.md").write_text("today\n")

    assert cli.main(["cleanup", str(workspace)]) == 0

    assert agent_runs == []


def test_an_archived_piece_without_a_description_starts_the_agent_kept_in_the_workspace(agent_runs, tmp_path):
    workspace = tmp_path / "work"
    bench_piece(workspace)

    assert cli.main(["cleanup", str(workspace), "-m", "deepseek/deepseek-v4-flash"]) == 0

    [argv] = agent_runs
    area = cleanup.archive_area(workspace.resolve())
    assert argv[argv.index("-C") + 1] == str(workspace.resolve())
    assert argv[argv.index("-a") + 1] == "cleanup"
    # The last -m is the agent's model; the first is `python -m js`.
    assert argv[len(argv) - argv[::-1].index("-m")] == "deepseek/deepseek-v4-flash"
    extras = [argv[i + 1] for i, word in enumerate(argv) if word == "--extra"]
    assert any(extra.startswith("JAIL_BIND=") and f"{area}:rw" in extra for extra in extras)
    assert str(area / "bench") in argv[argv.index("-p") + 1]


@pytest.mark.parametrize("flag", ["--no-model", "--dry-run"])
def test_no_model_and_dry_run_never_start_the_agent(agent_runs, tmp_path, flag):
    bench_piece(tmp_path / "work")

    assert cli.main(["cleanup", str(tmp_path / "work"), flag]) == 0

    assert agent_runs == []


def test_an_agent_that_fails_leaves_the_rules_done_and_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup_cli, "_run_agent", lambda argv: 1)
    said = []
    monkeypatch.setattr(msgs, "say", lambda message, **fields: said.append(message))
    workspace = tmp_path / "work"
    bench_piece(workspace)

    assert cli.main(["cleanup", str(workspace)]) == 0

    assert (cleanup.archive_area(workspace.resolve()) / "bench" / "results.csv").is_file()
    assert msgs.CLEANUP_AGENT_FAILED in said


def test_every_run_logs_to_the_one_log(agent_runs, tmp_path):
    for name in ("first", "second"):
        bench_piece(tmp_path / name)
        cli.main(["cleanup", str(tmp_path / name), "--no-model"])

    lines = cleanup.log_file().read_text().splitlines()
    assert any(str(tmp_path / "first" / "bench") in line for line in lines)
    assert any(str(tmp_path / "second" / "bench") in line for line in lines)
    assert sorted(path.name for path in paths.archive_root().rglob("*.log")) == ["cleanup.log"]


def test_the_agent_jail_drops_only_the_js_home_hide_and_shows_the_archive():
    area = paths.archive_root() / "work"

    extras = cleanup_cli.jail_extras(paths.work_dir(), area, ["~/.js", "~/.ssh"], ["/opt/models"], checkouts=[])

    assert extras == ['JAIL_HIDE=["~/.ssh"]',
                      f'JAIL_BIND=["/opt/models", "{area}:rw", "{cleanup.log_file()}:rw"]']


def test_the_agent_is_asked_about_a_checkout_once_until_its_piece_changes(agent_runs, tmp_path):
    workspace = tmp_path / "work"
    piece = workspace / "research"
    subprocess.run(["git", "init", "-q", str(piece / "llama.cpp")], check=True)
    (piece / "findings.md").write_text("q8 wins\n")

    cli.main(["cleanup", str(workspace)])
    cli.main(["cleanup", str(workspace)])

    assert len(agent_runs) == 1
    (piece / "findings.md").write_text("q8 wins, q4 close\n")
    cli.main(["cleanup", str(workspace)])
    assert len(agent_runs) == 2


def _git(*args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@x", *args],
                          check=True, capture_output=True, text=True).stdout


@needs_bwrap
def test_the_agent_can_compare_and_retire_a_worktree_it_was_left(monkeypatch, tmp_path):
    owner, workspace = tmp_path / "owner", tmp_path / "litter"
    _git("init", "-q", str(owner))
    _git("-C", str(owner), "commit", "-q", "--allow-empty", "-m", "base")
    piece = workspace / "sd-bench"
    piece.mkdir(parents=True)
    checkout = piece / "sd"
    _git("-C", str(owner), "worktree", "add", "-q", str(checkout), "-b", "agent")
    _git("-C", str(checkout), "commit", "-q", "--allow-empty", "-m", "work")
    area = cleanup.archive_area(workspace.resolve())
    area.mkdir(parents=True)
    cleanup.log_file().touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_warn_missing_binaries", lambda: None)
    extras = cleanup_cli.jail_extras(workspace, area, ["~/.js"], [], checkouts=[piece])
    results = {}

    def prompt(text, **kwargs):
        context = ToolContext(cwd=Path.cwd())
        config = run_assembly.from_env(None, save_session=False, agent_id=kwargs["agent"], sources=kwargs["sources"])
        turn_settings.install(context, config)
        results["ahead"] = shell.shell(f"git -C {checkout} rev-list --count master..HEAD", context=context)
        results["write"] = shell.shell(f"touch {owner}/planted; echo done", context=context)
        results["retire"] = shell.shell(f"git -C {checkout} worktree remove --force {checkout}", context=context)
        return 0

    monkeypatch.setattr(prompt_run, "run", prompt)
    argv = ["-C", str(workspace), "-a", "cleanup", "-n", "-q", "-p", "go"]
    for extra in extras:
        argv += ["--extra", extra]
    try:
        assert cli.main(argv) == 0
    finally:
        jail.leave()

    assert "\n1\n" in f"\n{results['ahead']}\n", results
    assert not (owner / "planted").exists(), results
    assert not checkout.exists(), results
    assert not (owner / ".git" / "worktrees" / "sd").exists(), results
    assert "agent" in _git("-C", str(owner), "branch", "--list", "agent")


@needs_bwrap
def test_the_agent_can_write_only_in_its_workspace_archive_and_log(monkeypatch, tmp_path):
    workspace = paths.work_dir()
    workspace.mkdir(parents=True, exist_ok=True)
    area = cleanup.archive_area(workspace.resolve())
    area.mkdir(parents=True)
    cleanup.log_file().touch()
    outside = tmp_path / "keep.txt"
    outside.write_text("benchmark\n")
    beside = paths.home() / "jsrc.local"
    beside.write_text("set X 1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_warn_missing_binaries", lambda: None)
    extras = cleanup_cli.jail_extras(workspace, area, ["~/.js", "~/.ssh"], [], checkouts=[])
    results = {}

    def prompt(text, **kwargs):
        context = ToolContext(cwd=Path.cwd())
        config = run_assembly.from_env(None, save_session=False, agent_id=kwargs["agent"], sources=kwargs["sources"])
        turn_settings.install(context, config)
        write = build_default_registry().resolve("write")
        results["archive"] = call_tool(write, {"path": str(area / "descriptions" / "x.md"), "content": "x\n"}, context)
        results["outside"] = call_tool(write, {"path": str(outside), "content": "gone\n", "overwrite": True}, context)
        results["shell"] = shell.shell(
            f"echo line >> {cleanup.log_file()}; rm -f {beside} {outside}; echo done", context=context)
        results["read"] = shell.shell(f"cat {beside}; echo done", context=context)
        return 0

    monkeypatch.setattr(prompt_run, "run", prompt)
    argv = ["-C", str(workspace), "-a", "cleanup", "-n", "-q", "-p", "go"]
    for extra in extras:
        argv += ["--extra", extra]
    try:
        assert cli.main(argv) == 0
    finally:
        jail.leave()

    assert (area / "descriptions" / "x.md").read_text() == "x\n", results
    assert cleanup.log_file().read_text() == "line\n", results
    assert results["outside"].startswith("ERROR"), results
    assert outside.read_text() == "benchmark\n" and beside.read_text() == "set X 1\n", results
    assert "set X 1" not in results["read"], results
