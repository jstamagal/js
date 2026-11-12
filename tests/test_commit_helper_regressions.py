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
