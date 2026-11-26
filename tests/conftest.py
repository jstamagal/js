"""Keep offline tests and their subprocesses out of the invoking user's profile,
and keep the models.dev catalog local."""

import os
import shutil
import signal
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime

import pytest

from js import jail, logins, model_metadata, paths


@pytest.fixture(autouse=True)
def isolated_user_profile(monkeypatch, tmp_path):
    # `cli.main` fills unset names from a .env into os.environ for the rest
    # of the process, as a run should; each test ends with the environment
    # it started with.
    environ = dict(os.environ)
    # Clear XDG overrides rather than fixing them to this HOME: individual
    # tests deliberately replace HOME again and expect the default layout.
    # Tests of explicit XDG configuration can still set their own overrides.
    # Playwright resolves its browser cache from HOME; keep pointing at the
    # real download so the browser_probe tests still find chromium.
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        real_cache = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
        if os.path.isdir(real_cache):
            monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", real_cache)
    # rustup's proxies (cargo, rust-analyzer) find their toolchains from HOME
    # the same way; keep them on the real install.
    real_rustup = os.path.join(os.path.expanduser("~"), ".rustup")
    if "RUSTUP_HOME" not in os.environ and os.path.isdir(real_rustup):
        monkeypatch.setenv("RUSTUP_HOME", real_rustup)
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
        if name.startswith(("JS_", "TYPESAFE_")):
            monkeypatch.delenv(name, raising=False)
    # A test that points the login store at its own directory leaves that
    # override behind for the next test on its worker; each test starts on
    # the store under this HOME.
    monkeypatch.setattr(logins, "_CONFIG_DIR_OVERRIDE", None)
    monkeypatch.setattr(paths, "_HOME", None)
    monkeypatch.setattr(paths, "_ISOLATE", None)
    # A command line one test ran must not shape the next test's settings.
    from js import config as _config

    _config.set_invocation(_config.InvocationSources())
    # `js -C` puts the whole process in a jail; one test's jail must not
    # confine the next test's tools.
    yield
    jail.leave()
    os.environ.clear()
    os.environ.update(environ)

@pytest.fixture(scope="session")
def _worker_cache_home(tmp_path_factory):
    """A cache home shared by one worker's tests, seeded from the invoking
    user's fontconfig cache. Copied, so the user's cache is only read."""
    root = tmp_path_factory.mktemp("cache-home")
    real_fonts = os.path.join(os.path.expanduser("~"), ".cache", "fontconfig")
    if os.path.isdir(real_fonts):
        shutil.copytree(real_fonts, root / "fontconfig", symlinks=True)
    return root


@pytest.fixture
def chromium_cache(monkeypatch, _worker_cache_home):
    """Chromium started in a fresh HOME rebuilds the fontconfig cache before it
    opens a page. XDG_CACHE_HOME points at the worker's cache home, so a
    launch finds a warm one."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(_worker_cache_home))


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
    return db, status, datetime.now(tz=UTC)


@pytest.fixture(scope="session")
def _worker_envctx_bin(tmp_path_factory):
    """Where one worker's envctx binary lives. Each test's fresh HOME would
    otherwise make every prompt expansion compile envctx.c again (~0.25s)."""
    return tmp_path_factory.mktemp("envctx") / "envctx.bin"


@pytest.fixture(autouse=True)
def shared_envctx_bin(monkeypatch, _worker_envctx_bin):
    monkeypatch.setenv("ENVCTX_BIN", str(_worker_envctx_bin))


@pytest.fixture(autouse=True)
def stop_signals_stay_as_they_were():
    """A test that leaves its own SIGINT or SIGTERM handler behind changes
    how this worker stops: GNU `timeout` signals the whole process group, and
    a handler that turns SIGTERM into ^C makes a timed-out run read as an
    interrupt. Each test ends with the handlers it started with."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in before.items():
        if signal.getsignal(sig) is not handler and handler is not None:
            signal.signal(sig, handler)


@pytest.fixture(autouse=True)
def fresh_fset(monkeypatch):
    """Each test starts with no /fset entry set, no live settings bound,
    colour on whatever NO_COLOR the invoking shell has, and the banner it
    ends with put back."""
    from js import fset, messages

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(messages, "BANNER", messages.BANNER)
    yield
    fset.reset()
    fset.bind_settings(None)


@pytest.fixture(autouse=True)
def local_model_catalog(monkeypatch, fresh_model_catalog):
    """Every test reads the session's fresh catalog, so no lookup downloads
    models.dev. A refresh that reaches the download fails instead. The
    catalog path the models.dev package reads is reset per test, since
    model_metadata activates a database by writing it to the environment."""
    db, status_path, refreshed_at = fresh_model_catalog
    monkeypatch.setattr(model_metadata, "_custom_db_path", lambda: db)
    monkeypatch.setattr(model_metadata, "_status_file_path", lambda: status_path)
    monkeypatch.setenv(model_metadata.modelsdotdev_data.DATABASE_PATH_ENV, str(db))
    if not status_path.exists():
        model_metadata.record_refreshed(db, refreshed_at=refreshed_at)

    def no_download(source):
        raise RuntimeError(f"offline test suite: not downloading {source}")

    monkeypatch.setattr(model_metadata.modelsdotdev_sync, "_load_providers", no_download)
    monkeypatch.setattr(model_metadata, "_start_refresher", lambda age_max: None)
    monkeypatch.setattr(model_metadata, "_seen_db", [])


@pytest.fixture
def home_model_catalog(fresh_model_catalog):
    """Put the session's catalog where a js process started in this test's
    HOME looks for it, so that process does not download models.dev either."""
    db, _status_path, refreshed_at = fresh_model_catalog
    target = paths.model_catalog_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(db)
    # local_model_catalog points this process's status file at the session's;
    # the home's is the one a js process started here reads.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(model_metadata, "_status_file_path", paths.model_catalog_status_path)
        model_metadata.record_refreshed(target, refreshed_at=refreshed_at)
    return target
