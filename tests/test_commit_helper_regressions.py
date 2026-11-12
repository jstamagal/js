"""Commit-helper command-line failures retain meaningful exit status."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_helper(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "js.commit_helper", *arguments],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


@pytest.mark.parametrize("arguments, status, output", [
    (["bogus-verb"], 2, "stderr"),
    ([], 2, "stderr"),
    (["--help"], 0, "stdout"),
    (["-h"], 0, "stdout"),
])
def test_cli_help_and_invalid_command_exit_status(arguments, status, output):
    result = run_helper(*arguments)
    assert result.returncode == status
    assert getattr(result, output).strip()
    other = "stdout" if output == "stderr" else "stderr"
    assert not getattr(result, other)


@pytest.mark.parametrize("payload", [b"\xff\xfe bad\n", b"valid prefix \xed\xa0\x80\n"])
def test_invalid_utf8_commit_message_is_reported_without_mutating_repo(tmp_path, payload):
    def git(*arguments):
        return subprocess.run(["git", "-C", str(tmp_path), *arguments], capture_output=True, check=True).stdout

    git("init", "-q")
    git("config", "user.name", "John Stamagal")
    git("config", "user.email", "jstamagal@gmail.com")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("base\n")
    git("add", "tracked.txt")
    git("commit", "-qm", "base")
    tracked.write_text("changed\n")
    git("add", "tracked.txt")
    message = tmp_path / "message.txt"
    message.write_bytes(payload)
    head = git("rev-parse", "HEAD")
    index = (tmp_path / ".git" / "index").read_bytes()

    result = run_helper("-C", str(tmp_path), "commit", str(message))

    assert result.returncode == 2
    assert result.stderr.strip()
    assert not result.stdout
    assert "Traceback" not in result.stderr
    assert git("rev-parse", "HEAD") == head
    assert (tmp_path / ".git" / "index").read_bytes() == index
    assert tracked.read_text() == "changed\n"
