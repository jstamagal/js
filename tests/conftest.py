"""Keep offline tests and their subprocesses out of the invoking user's profile."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_user_profile(monkeypatch, tmp_path):
    # Clear XDG overrides rather than fixing them to this HOME: individual
    # tests deliberately replace HOME again and expect the default layout.
    # Tests of explicit XDG configuration can still set their own overrides.
    # Playwright resolves its browser cache from HOME; keep pointing at the
    # real download so the browser_probe tests still find chromium.
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        real_cache = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
        if os.path.isdir(real_cache):
            monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", real_cache)
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_CONFIG_DIRS",
        "XDG_DATA_DIRS",
    ):
        monkeypatch.delenv(name, raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    for name in tuple(os.environ):
        if name.startswith("JS_"):
            monkeypatch.delenv(name, raising=False)