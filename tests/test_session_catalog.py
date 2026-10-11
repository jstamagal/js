from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from js import memory
from js.session_catalog import (
    acquire_session,
    catalog_sessions,
    record_session_start,
    release_session,
    session_in_flight,
)


def _message(path: Path, role: str, content: str) -> None:
    memory.append_message(path, {"role": role, "content": content})


def test_catalog_recurses_agents_and_nested_sessions_with_exact_stats(tmp_path):
    root = tmp_path / "sessions"
    generated = root / "alpha" / "20260101-000000-deadbeef.jsonl"
    nested = root / "alpha" / "mcp" / "lazy" / "slice02.jsonl"
    other = root / "worker" / "derived" / "opaque.jsonl"

    _message(generated, "user", "one")
    _message(generated, "assistant", "answer")
    _message(nested, "user", "two")
    memory.append_mark(nested, "session_reset")
    _message(nested, "user", "three")
    _message(other, "assistant", "worker answer")

    expected = {
        path: (path.stat().st_size, path.stat().st_mtime)
        for path in (generated, nested, other)
    }
    records = catalog_sessions(root)
    by_key = {(record["agent"], record["name"]): record for record in records}

    assert set(by_key) == {
        ("alpha", "20260101-000000-deadbeef"),
        ("alpha", "mcp/lazy/slice02"),
        ("worker", "derived/opaque"),
    }
    assert by_key[("alpha", "20260101-000000-deadbeef")]["user_turns"] == 1
    assert by_key[("alpha", "mcp/lazy/slice02")]["user_turns"] == 2
    assert by_key[("worker", "derived/opaque")]["user_turns"] == 0
    for record in records:
        size, mtime = expected[Path(record["path"])]
        assert record["size"] == size
        assert record["mtime"] == mtime
        assert record["in_flight"] is False


def test_metadata_round_trips_without_becoming_a_model_message(tmp_path):
    root = tmp_path / "sessions"
    session = root / "worker" / "derived" / "key.jsonl"
    _message(session, "user", "hello")
    record_session_start(
        session,
        cwd=tmp_path / "project" / "missing" / "..",
        caller_key="caller/channel/42",
        job_id=17,
    )
    _message(session, "assistant", "world")

    before = session.read_bytes()
    record = catalog_sessions(root)[0]

    assert record["cwd"] == str((tmp_path / "project").resolve(strict=False))
    assert record["caller_key"] == "caller/channel/42"
    assert record["job_id"] == 17
    assert record["user_turns"] == 1
    assert memory.load_messages(session) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    assert session.read_bytes() == before


def test_old_session_lists_with_empty_metadata(tmp_path):
    session = tmp_path / "sessions" / "old-agent" / "old.jsonl"
    _message(session, "user", "legacy")

    record = catalog_sessions(tmp_path / "sessions")[0]

    assert record["cwd"] is None
    assert record["caller_key"] is None
    assert record["job_id"] is None


def test_liveness_tracks_acquire_release_and_concurrent_opens(tmp_path):
    session = tmp_path / "sessions" / "agent" / "named.jsonl"
    session.parent.mkdir(parents=True)
    session.touch()

    first = acquire_session(session)
    second = acquire_session(session)
    assert session_in_flight(session) is True

    release_session(first)
    assert session_in_flight(session) is True

    second.release()
    assert session_in_flight(session) is False


def test_stale_pid_is_pruned_and_mtime_never_implies_liveness(tmp_path):
    session = tmp_path / "sessions" / "agent" / "stale.jsonl"
    session.parent.mkdir(parents=True)
    session.touch()
    os.utime(session, None)

    state_path = session.parent / f".{session.name}.liveness.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "opens": [
                    {
                        "token": "crashed",
                        "pid": 999_999_999,
                        "process_start": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert session_in_flight(session) is False
    assert catalog_sessions(tmp_path / "sessions")[0]["in_flight"] is False
    # Inspection is read-only; the next acquire prunes the dead opener.
    acquire_session(session).release()
    assert not state_path.exists()


def test_exited_unreaped_opener_is_not_in_flight(tmp_path):
    session = tmp_path / "sessions" / "agent" / "crashed.jsonl"
    session.parent.mkdir(parents=True)
    session.touch()
    acquired = tmp_path / "acquired"
    code = """
import os
from pathlib import Path
from js.session_catalog import acquire_session

acquire_session(Path(os.environ["SESSION_FILE"]))
Path(os.environ["ACQUIRED_FILE"]).touch()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        env={**os.environ, "SESSION_FILE": str(session), "ACQUIRED_FILE": str(acquired)},
    )
    try:
        stat_path = Path(f"/proc/{process.pid}/stat")
        deadline = time.monotonic() + 5
        state = None
        while time.monotonic() < deadline:
            if acquired.exists():
                raw = stat_path.read_text(encoding="utf-8")
                state = raw[raw.rfind(")") + 2 :].split()[0]
                if state == "Z":
                    break
            time.sleep(0.01)
        assert acquired.exists()
        assert state == "Z"
        assert session_in_flight(session) is False
    finally:
        process.wait(timeout=5)


def test_context_manager_releases_liveness(tmp_path):
    session = tmp_path / "session.jsonl"
    session.touch()

    with acquire_session(session):
        assert session_in_flight(session) is True

    assert session_in_flight(session) is False


# --- read-only, tri-state liveness (ALL-CLAIMS session_catalog.py:133) -------------

import pytest  # noqa: E402

from js import home  # noqa: E402
from js import session_catalog as C  # noqa: E402


def _sidecars(session: Path) -> tuple[Path, Path]:
    return C._sidecar_paths(session)


def _state_live(session: Path) -> None:
    state, _ = _sidecars(session)
    state.write_text(json.dumps({"version": C._LIVENESS_VERSION, "opens": [
        {"pid": os.getpid(), "process_start": C._process_start(os.getpid()), "token": "alive"}]}))


@pytest.fixture
def session(tmp_path):
    path = tmp_path / "sessions" / "agent" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("")
    return path


def test_no_state_file_is_idle_and_inspecting_creates_no_lock(session):
    assert session_in_flight(session) is False
    assert not _sidecars(session)[1].exists()


@pytest.mark.parametrize("data", [
    b"{", b"\xff", b"null", b"[]", b'{"version":999,"opens":[]}',
    b'{"version":1,"opens":[42]}', b'{"version":1,"opens":[{"pid":"bad"}]}',
    b'{"version":1,"opens":[{"pid":true}]}', b'{"version":1}',
    b'{"version":1,"opens":[{"pid":0}]}', b'{"version":1,"opens":[{"pid":-3}]}',
    b'{"version":1,"opens":[{"pid":999999999,"process_start":7}]}',
    b'{"version":1,"opens":[{"pid":1' + b'0' * 100 + b'}]}',
])
def test_unparseable_state_is_unknown(session, data):
    state, lock = _sidecars(session)
    state.write_bytes(data)
    assert session_in_flight(session) is None
    assert not lock.exists()


def test_a_read_only_directory_lists_and_reports_live_without_a_lock(session):
    _state_live(session)
    session.parent.chmod(0o555)
    try:
        assert session_in_flight(session) is True
        assert catalog_sessions(session.parent.parent)[0]["in_flight"] is True
        assert not _sidecars(session)[1].exists()
    finally:
        session.parent.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_an_unreadable_state_is_unknown_and_the_listing_survives(session):
    _state_live(session)
    state, _ = _sidecars(session)
    state.chmod(0)
    try:
        assert session_in_flight(session) is None
        assert catalog_sessions(session.parent.parent)[0]["in_flight"] is False
    finally:
        state.chmod(0o600)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_an_unsearchable_parent_is_unknown_not_missing(session):
    _state_live(session)
    session.parent.chmod(0)
    try:
        assert session_in_flight(session) is None
    finally:
        session.parent.chmod(0o700)


@pytest.mark.parametrize("answer, refused", [(None, True), (True, True), (False, False)])
def test_home_refuses_to_move_a_journal_it_cannot_prove_idle(session, monkeypatch, answer, refused):
    monkeypatch.setattr(C, "session_in_flight", lambda _path: answer)
    assert home._in_use(session) is refused


@pytest.mark.parametrize("data", [b'{"version":1,"opens":[]}', b'{"version":1,"opens":[{"pid":999999999}]}'])
def test_well_formed_state_with_no_live_opener_is_idle(session, data):
    _sidecars(session)[0].write_bytes(data)
    assert session_in_flight(session) is False


@pytest.mark.parametrize("data", [b'{"version":1,"opens":[42]}', b'{"version":1,"opens":[{"pid":"bad"}]}'])
def test_home_refuses_a_journal_whose_state_rows_are_unreadable(session, data):
    _sidecars(session)[0].write_bytes(data)
    assert home._in_use(session) is True


def test_a_symlink_loop_is_unknown_not_a_crash(tmp_path):
    loop = tmp_path / "loop.jsonl"
    loop.symlink_to(loop)
    # resolve() sits inside the guarded read; on this Python it returns the
    # path, which has no state file.
    assert session_in_flight(loop) is False


def test_a_live_pid_with_a_malformed_start_is_unknown(session):
    _sidecars(session)[0].write_text(json.dumps(
        {"version": 1, "opens": [{"pid": os.getpid(), "process_start": 42}]}))
    assert session_in_flight(session) is None
    assert home._in_use(session) is True
