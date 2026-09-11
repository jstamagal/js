"""The shared profile fixture must also protect subprocess and HOME-only tests."""

import os
import subprocess
import sys

from js import paths


def test_default_paths_follow_a_replaced_home(monkeypatch, tmp_path):
    home = tmp_path / "another-home"
    monkeypatch.setenv("HOME", str(home))
    assert paths.config_dir() == home / ".config" / "js"
    assert paths.sessions_root() == home / ".local" / "share" / "js" / "sessions"
    assert "JS_SESSION" not in os.environ


def test_subprocess_inherits_isolated_profile(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", "from js.paths import sessions_root; print(sessions_root())"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    assert result.stdout.strip() == str(tmp_path / ".local" / "share" / "js" / "sessions")


def test_explicit_xdg_override_remains_testable(monkeypatch, tmp_path):
    data = tmp_path / "explicit-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    assert paths.sessions_root() == data / "js" / "sessions"