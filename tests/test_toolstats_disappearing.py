"""Latest-session lookup skips candidates removed during discovery."""

import json
import os
from pathlib import Path

import pytest

from js import toolstats


def session(data_dir, name, modified, agent="worker"):
    path = data_dir / "sessions" / "project" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"kind": "session_metadata", "agent": agent}) + "\n")
    os.utime(path, (modified, modified))
    return path


@pytest.mark.parametrize("agent", [None, "worker"])
def test_disappearing_stat_candidate_does_not_hide_next_session(tmp_path, monkeypatch, agent):
    older = session(tmp_path, "older", 1)
    newest = session(tmp_path, "newest", 3)
    session(tmp_path, "other", 2, "different")
    real_stat = Path.stat
    stat_counts = {}

    def stat(path, *args, **kwargs):
        if path.suffix == ".jsonl":
            stat_counts[path] = stat_counts.get(path, 0) + 1
        if path == newest:
            newest.unlink()
            raise FileNotFoundError(str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    expected = older if agent else tmp_path / "sessions" / "project" / "other.jsonl"
    assert toolstats.latest_session(tmp_path, agent) == expected
    assert set(stat_counts.values()) == {1}


@pytest.mark.parametrize("keep_older", [False, True])
def test_disappearing_agent_read_skips_to_next_or_none(tmp_path, monkeypatch, keep_older):
    newest = session(tmp_path, "newest", 3)
    older = session(tmp_path, "older", 1) if keep_older else None
    session(tmp_path, "wrong-agent", 2, "different")
    real_read = Path.read_text

    def read(path, *args, **kwargs):
        if path == newest:
            newest.unlink()
            raise FileNotFoundError(str(path))
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert toolstats.latest_session(tmp_path, "worker") == older


def test_latest_retains_order_and_last_agent_filter(tmp_path):
    latest = session(tmp_path, "latest", 3, "other")
    matching = session(tmp_path, "matching", 2, "worker")
    session(tmp_path, "oldest", 1, "worker")
    assert toolstats.latest_session(tmp_path, None) == latest
    assert toolstats.latest_session(tmp_path, "worker") == matching
    assert toolstats.latest_session(tmp_path, "absent") is None
