"""The suite runs one worker per core."""

import re
from pathlib import Path

import xdist.plugin

ROOT = Path(__file__).resolve().parent.parent


def test_no_plugin_or_conftest_changes_the_auto_worker_count(pytestconfig):
    resolved = pytestconfig.pluginmanager.hook.pytest_xdist_auto_num_workers(config=pytestconfig)
    assert resolved == xdist.plugin.pytest_xdist_auto_num_workers(pytestconfig)


def test_just_test_and_bare_pytest_ask_for_every_core():
    recipe = re.search(r"^test .*?(?=^\S)", (ROOT / "justfile").read_text(), re.M | re.S)
    assert recipe and "-n logical" in recipe.group(0)
    assert "-n logical" in (ROOT / "pyproject.toml").read_text()
