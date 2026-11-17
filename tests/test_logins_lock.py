"""The login store lock.

The sidecar lock serializes every writer of logins.toml and
models-cache.json across processes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from js import logins, paths


@pytest.fixture
def store(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    yield tmp_path
    logins.set_config_dir(paths.login_store_dir())


def _hold_in_subprocess(lock: Path, seconds: float) -> subprocess.Popen:
    """Another process holds the sidecar flock for ``seconds``; returns once it does."""
    code = textwrap.dedent(
        f"""
        import fcntl, os, sys, time
        fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        print("held", flush=True)
        time.sleep({seconds})
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_sync_lock_waits_for_another_process_then_proceeds(store):
    proc = _hold_in_subprocess(logins.lock_path(), 0.5)
    try:
        t0 = time.monotonic()
        with logins.hold_login_lock(timeout_s=5):
            waited = time.monotonic() - t0
        assert waited >= 0.3
    finally:
        proc.wait()


def test_sync_lock_times_out_instead_of_blocking_forever(store):
    proc = _hold_in_subprocess(logins.lock_path(), 2.0)
    try:
        with pytest.raises(logins.LoginStoreBusyError):
            with logins.hold_login_lock(timeout_s=0.3):
                pass
    finally:
        proc.kill()
        proc.wait()


def test_lock_is_reentrant_so_save_login_works_under_a_held_lock(store):
    with logins.hold_login_lock():
        logins.save_login(logins.Login(provider_id="a", provider_api_key="k"))
        with logins.hold_login_lock():
            logins.save_login(logins.Login(provider_id="b", provider_api_key="k"))
    assert set(logins.load_logins()) == {"a", "b"}


def test_lock_held_on_store_a_is_not_a_lock_on_store_b(tmp_path: Path):
    store_a, store_b = tmp_path / "a", tmp_path / "b"
    store_b.mkdir()
    try:
        logins.set_config_dir(store_a)
        with logins.hold_login_lock():
            logins.set_config_dir(store_b)
            proc = _hold_in_subprocess(logins.lock_path(), 2.0)
            try:
                with pytest.raises(logins.LoginStoreBusyError):
                    with logins.hold_login_lock(timeout_s=0.3):
                        pass
            finally:
                proc.kill()
                proc.wait()
            logins.set_config_dir(store_a)
    finally:
        logins.set_config_dir(paths.login_store_dir())


def test_nested_hold_survives_a_store_switch_inside(tmp_path: Path):
    # The inner level must release the store it entered, not whatever the
    # config dir says at exit time.
    store_a, store_b = tmp_path / "a", tmp_path / "b"
    try:
        logins.set_config_dir(store_a)
        with logins.hold_login_lock():
            with logins.hold_login_lock():
                logins.set_config_dir(store_b)
            logins.set_config_dir(store_a)
        assert logins._owner.holders == {}
        # And a foreign holder of A's sidecar is honoured afterwards (no stale ownership).
        proc = _hold_in_subprocess(logins.lock_path(), 2.0)
        try:
            with pytest.raises(logins.LoginStoreBusyError):
                with logins.hold_login_lock(timeout_s=0.3):
                    pass
        finally:
            proc.kill()
            proc.wait()
    finally:
        logins.set_config_dir(paths.login_store_dir())


def _concurrent_savers(store: Path, n: int) -> None:
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from pathlib import Path
        from modelsdotdev._internal import sync

        download_requests = []

        def no_download(source):
            download_requests.append(source)
            raise AssertionError("offline login-lock child requested catalog download")

        sync._load_providers = no_download
        from js import logins
        logins.set_config_dir(Path({str(store)!r}))
        logins.save_login(logins.Login(provider_id=f"p{{sys.argv[1]}}", provider_api_key="k"))
        logins.cache_models(f"p{{sys.argv[1]}}", ["m"])
        assert not download_requests, "offline login-lock child requested catalog download"
        """
    )
    procs = []
    try:
        for i in range(n):
            procs.append(subprocess.Popen([sys.executable, "-c", code, str(i)]))
        deadline = time.monotonic() + 15
        codes = [proc.wait(timeout=max(0, deadline - time.monotonic())) for proc in procs]
        assert codes == [0] * n
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        for proc in procs:
            proc.wait()


def test_concurrent_processes_do_not_lose_each_others_logins_or_caches(store, home_model_catalog):
    _concurrent_savers(store, 8)
    assert set(logins.load_logins()) == {f"p{i}" for i in range(8)}
    assert set(logins.load_model_cache()) == {f"p{i}" for i in range(8)}


def test_model_cache_write_is_atomic_and_never_overwrites_an_unreadable_file(store):
    logins.save_model_cache({"alpha": ["a1"], "beta": ["b1"]})
    cache = store / "models-cache.json"
    raw = cache.read_text()
    cache.write_text(raw[: len(raw) // 2])  # a torn file, as a killed writer leaves it
    assert logins.load_model_cache() == {}  # readers still degrade quietly
    with pytest.raises(logins._CacheUnreadable):
        logins.save_model_cache({"gamma": ["g1"]})
    assert cache.read_text() == raw[: len(raw) // 2]  # bytes untouched for the human
    names = {entry.name for entry in store.iterdir() if entry.is_file()}
    assert not any(name.endswith(".tmp") for name in names)


def test_json_cache_file_shape_unchanged(store):
    logins.cache_models("p", ["m1", "m2"])
    assert json.loads((store / "models-cache.json").read_text()) == {"p": ["m1", "m2"]}
