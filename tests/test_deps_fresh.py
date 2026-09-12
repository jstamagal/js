"""Pin the justfile freshness gate against a throwaway repo.

The gate measures days since uv.lock last changed, so the test backdates its
own commit rather than reading this checkout's history.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
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


def _repo_with_lock_committed_days_ago(tmp_path: Path, days: int) -> Path:
    """A repo whose uv.lock commit is dated `days` ago, with newer work on top.

    The gate reads uv.lock's own commit date, so the later commit proves a busy
    day cannot make a freshly relocked tree look stale.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    # GIT_COMMITTER_DATE wants a real timestamp, not an approxidate phrase.
    stamp = f"{int(time.time()) - days * 86400} +0000"
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "lock"],
        cwd=repo,
        env={**os.environ, **GIT_IDENTITY,
             "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
        capture_output=True, text=True, check=True,
    )
    (repo / "work.txt").write_text("x\n", encoding="utf-8")
    _commit(repo, "work today")
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


def test_a_lock_changed_today_is_fresh(tmp_path: Path):
    repo = _repo_with_lock_committed_days_ago(tmp_path, 0)

    result = _run_deps_fresh(repo)

    assert result.returncode == 0


def test_a_lock_untouched_past_the_limit_fails(tmp_path: Path):
    repo = _repo_with_lock_committed_days_ago(tmp_path, 30)

    result = _run_deps_fresh(repo)

    assert result.returncode == 1
    assert "30 days" in result.stderr
    assert "just upgrade" in result.stderr


def test_a_raised_limit_accepts_an_older_lock(tmp_path: Path):
    repo = _repo_with_lock_committed_days_ago(tmp_path, 30)

    result = _run_deps_fresh(repo, "--set", "deps-stale-days", "60")

    assert result.returncode == 0
