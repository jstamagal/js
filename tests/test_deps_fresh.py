"""Pin the justfile freshness gate against a throwaway repo.

The gate counts commits since uv.lock last changed, so the test builds its own
history with a known number of commits rather than reading this checkout's.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

JUSTFILE = Path(__file__).resolve().parents[1] / "justfile"

pytestmark = pytest.mark.skipif(shutil.which("just") is None, reason="just is not on PATH")

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "deps-fresh test",
    "GIT_AUTHOR_EMAIL": "deps-fresh@example.invalid",
    "GIT_COMMITTER_NAME": "deps-fresh test",
    "GIT_COMMITTER_EMAIL": "deps-fresh@example.invalid",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **GIT_IDENTITY},
        capture_output=True,
        text=True,
        check=True,
    )


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)


def _repo_with_commits_since_lock(tmp_path: Path, commits: int) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _commit(repo, "lock")
    for index in range(commits):
        (repo / "work.txt").write_text(f"{index}\n", encoding="utf-8")
        _commit(repo, f"change {index}")
    return repo


def _run_deps_fresh(repo: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "just",
            *flags,
            "--justfile",
            str(JUSTFILE),
            "--working-directory",
            str(repo),
            "deps-fresh",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_four_commits_since_lock_pass(tmp_path: Path):
    repo = _repo_with_commits_since_lock(tmp_path, 4)

    result = _run_deps_fresh(repo)

    assert result.returncode == 0


def test_five_commits_since_lock_fail(tmp_path: Path):
    repo = _repo_with_commits_since_lock(tmp_path, 5)

    result = _run_deps_fresh(repo)

    assert result.returncode == 1
    assert "5" in result.stderr
    assert "stale" in result.stderr


def test_raised_limit_allows_five_commits(tmp_path: Path):
    repo = _repo_with_commits_since_lock(tmp_path, 5)

    result = _run_deps_fresh(repo, "--set", "deps-stale-limit", "10")

    assert result.returncode == 0
