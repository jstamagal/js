"""Keep offline tests and their subprocesses out of the invoking user's profile,
and keep the models.dev catalog local."""

import os
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest

from js import model_metadata


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

@pytest.fixture(scope="session")
def fresh_model_catalog(tmp_path_factory):
    """The bundled models.dev catalog, recorded as refreshed at session start.

    Built once per worker. The bundled copy lacks the release-date table a
    refreshed catalog carries, so the table is added empty."""
    root = tmp_path_factory.mktemp("modelsdotdev")
    db = root / "modelsdotdev.sqlite"
    shutil.copyfile(model_metadata.bundled_db_path(), db)
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS model_release_dates "
            "(full_id TEXT PRIMARY KEY, release_date TEXT NOT NULL)"
        )
        connection.commit()
    status = root / "status.json"
    return db, status, model_metadata._status_from_db(db, refreshed_at=datetime.now(tz=UTC))


@pytest.fixture(autouse=True)
def local_model_catalog(monkeypatch, fresh_model_catalog):
    """Every test reads the session's fresh catalog, so no lookup downloads
    models.dev. A refresh that reaches the download fails instead."""
    db, status_path, status = fresh_model_catalog
    monkeypatch.setattr(model_metadata, "_custom_db_path", lambda: db)
    monkeypatch.setattr(model_metadata, "_status_file_path", lambda: status_path)
    if not status_path.exists():
        model_metadata._write_status_file(status)

    def no_download(source):
        raise RuntimeError(f"offline test suite: not downloading {source}")

    monkeypatch.setattr(model_metadata.modelsdotdev_sync, "_load_providers", no_download)
