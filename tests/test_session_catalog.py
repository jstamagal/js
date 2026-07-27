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
    assert not state_path.exists()
    assert catalog_sessions(tmp_path / "sessions")[0]["in_flight"] is False


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
