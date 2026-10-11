"""The login store lock (ALL-CLAIMS logins.py:271 MED, :428 MED; bakari v3.3).

The sidecar lock serializes every writer of logins.toml and
models-cache.json across processes, sync and async, without ever blocking
the event loop, and a waiter can be cancelled without holding anything.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
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


def test_async_lock_does_not_block_the_loop_and_is_cancel_safe(store):
    proc = _hold_in_subprocess(logins.lock_path(), 0.8)

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        tick_task = asyncio.create_task(ticker())

        async def waiter():
            async with logins.ahold_login_lock(timeout_s=5):
                return "acquired"

        # A waiter cancelled mid-poll holds nothing afterwards.
        cancelled = asyncio.create_task(waiter())
        await asyncio.sleep(0.1)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        # The loop kept turning while the second waiter polled.
        before = ticks
        result = await waiter()
        tick_task.cancel()
        assert result == "acquired"
        assert ticks - before > 10
        # And the lock is free again: a fresh fd can take it at once.
        fd = os.open(logins.lock_path(), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    try:
        asyncio.run(scenario())
    finally:
        proc.wait()


def test_lock_is_reentrant_so_save_login_works_under_a_held_lock(store):
    with logins.hold_login_lock():
        logins.save_login(logins.Login(provider_id="a", provider_api_key="k"))
        with logins.hold_login_lock():
            logins.save_login(logins.Login(provider_id="b", provider_api_key="k"))
    assert set(logins.load_logins()) == {"a", "b"}


def _concurrent_savers(store: Path, n: int) -> None:
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from pathlib import Path
        from js import logins
        logins.set_config_dir(Path({str(store)!r}))
        logins.save_login(logins.Login(provider_id=f"p{{sys.argv[1]}}", provider_api_key="k"))
        logins.cache_models(f"p{{sys.argv[1]}}", ["m"])
        """
    )
    procs = [subprocess.Popen([sys.executable, "-c", code, str(i)]) for i in range(n)]
    for proc in procs:
        assert proc.wait() == 0


def test_concurrent_processes_do_not_lose_each_others_logins_or_caches(store):
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


def test_get_login_prefers_a_fresher_memory_login_until_disk_catches_up(store):
    disk = logins.Login(provider_id="xai-oauth", provider_api_key="A1", xai_refresh_token="R1", xai_token_expires=100.0)
    logins.save_login(disk)
    memory = logins.Login(provider_id="xai-oauth", provider_api_key="A2", xai_refresh_token="R2", xai_token_expires=200.0)
    logins.note_memory_login(memory)
    assert logins.get_login("xai-oauth").xai_refresh_token == "R2"
    # Disk catches up (any process saved something newer): disk wins and the overlay is dropped.
    logins.save_login(logins.Login(provider_id="xai-oauth", provider_api_key="A3", xai_refresh_token="R3", xai_token_expires=300.0))
    assert logins.get_login("xai-oauth").xai_refresh_token == "R3"
    assert "xai-oauth" not in logins._overlay
    # A logout wins over memory too.
    logins.note_memory_login(memory)
    logins.remove_login("xai-oauth")
    assert logins.get_login("xai-oauth") is None
    assert "xai-oauth" not in logins._overlay


def test_json_cache_file_shape_unchanged(store):
    logins.cache_models("p", ["m1", "m2"])
    assert json.loads((store / "models-cache.json").read_text()) == {"p": ["m1", "m2"]}
