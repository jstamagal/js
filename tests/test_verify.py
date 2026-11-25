"""Verification checks the active configuration directories."""

import pytest

from js import paths
from js.verify import verify


def test_verify_ignores_configuration_inside_home_worktrees(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    nested = paths.home() / "worktrees" / "x" / "jsrc"
    nested.parent.mkdir(parents=True)
    nested.write_text("set TURN_ROUNDS_MAX invalid\n")

    errors = verify()

    assert not any(str(nested) in error for error in errors)


@pytest.mark.parametrize("layer", ["home", "project"])
def test_verify_checks_presets_in_configuration_directory(monkeypatch, tmp_path, layer):
    monkeypatch.chdir(tmp_path)
    directory = paths.home() if layer == "home" else tmp_path / ".js"
    directory.mkdir(parents=True)
    preset = directory / "jsrc.broken"
    preset.write_text("set TURN_ROUNDS_MAX invalid\n")

    errors = verify()

    assert any(str(preset) in error and "TURN_ROUNDS_MAX" in error for error in errors)
