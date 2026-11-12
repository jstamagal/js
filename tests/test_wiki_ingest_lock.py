from __future__ import annotations

import fcntl
import subprocess
import sys

import pytest

from js.toolkit.core import ToolContext
from js.toolkit.wiki import ops


def git(vault, *args):
    return subprocess.run(["git", "-C", str(vault), *args], check=True, capture_output=True, text=True).stdout


@pytest.mark.parametrize("lock_state", ["untracked", "staged", "tracked"])
def test_ingest_commits_pages_but_not_vault_lock(tmp_path, monkeypatch, lock_state):
    vault = tmp_path / "vault"
    vault.mkdir()
    git(vault, "init")
    git(vault, "config", "user.name", "John Stamagal")
    git(vault, "config", "user.email", "jstamagal@gmail.com")
    (vault / "PURPOSE.md").write_text("fixture wiki")
    (vault / ".gitignore").write_text("*.temporary\n")
    (vault / "assets").mkdir()
    (vault / "assets" / ".wiki.lock").write_text("unrelated nested asset")
    lock = vault / ".wiki.lock"
    lock.write_text("lock sentinel")
    inode = lock.stat().st_ino
    if lock_state in {"staged", "tracked"}:
        git(vault, "add", ".wiki.lock")
    if lock_state == "tracked":
        git(vault, "commit", "-m", "previous lock artifact")
    unit = vault / "inbox" / "u1"
    unit.mkdir(parents=True)
    (unit / "data.md").write_text("intended archive")
    original_run = ops.run
    observed_lock = []

    def check_lock(cmd, context, *args, **kwargs):
        if "add" in cmd:
            assert lock.stat().st_ino == inode
            with lock.open("rb") as probe:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            probe = subprocess.run(
                [sys.executable, "-c", "import fcntl, sys\nf=open(sys.argv[1], 'rb')\ntry: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept BlockingIOError: sys.exit(0)\nsys.exit(1)", str(lock)],
                capture_output=True, text=True, timeout=5,
            )
            assert probe.returncode == 0, probe.stderr
            observed_lock.append(True)
        return original_run(cmd, context, *args, **kwargs)

    monkeypatch.setattr(ops, "run", check_lock)
    result = ops.wiki_finish_ingest(str(vault), "u1", "fixture ingest", context=ToolContext(cwd=vault))

    assert not result.startswith("ERROR:")
    tracked = git(vault, "ls-files").splitlines()
    assert ".wiki.lock" not in tracked
    assert {"PURPOSE.md", "Clippings/u1/data.md", "log.md", ".gitignore", "assets/.wiki.lock"} <= set(tracked)
    assert observed_lock
    assert lock.stat().st_ino == inode
    assert lock.read_text() == "lock sentinel"


def test_failed_lock_index_cleanup_does_not_continue_commit(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    git(vault, "init")
    context = ToolContext(cwd=vault)
    original_run = ops.run
    commands = []

    def fail_remove(cmd, context, *args, **kwargs):
        commands.append(cmd)
        if "rm" in cmd:
            return 1, "", "fixture index failure"
        return original_run(cmd, context, *args, **kwargs)

    monkeypatch.setattr(ops, "run", fail_remove)
    result = ops._commit(vault, "fixture", context)

    assert result.startswith("ERROR:")
    assert not any("add" in cmd or "commit" in cmd for cmd in commands)
