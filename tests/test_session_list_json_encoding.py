"""Session-list JSON remains UTF-8 even when journal strings contain surrogates."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from js import session_store
from js.session_catalog import append_title, record_session_start


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("field, value", [
    ("title", "title café 文 🚲 \udcff"),
    ("title", "title \ud800"),
    ("cwd", "directory \udcff"),
    ("agent", "worker \udcff"),
    ("title", "ordinary café 文 🚲"),
])
def test_actual_cli_session_list_json_is_valid_utf8(tmp_path, monkeypatch, field, value):
    monkeypatch.chdir(tmp_path)
    path = session_store.reserve(session_store.folder_for(tmp_path))
    cwd = str(tmp_path / value) if field == "cwd" else str(tmp_path)
    agent = value if field == "agent" else "worker"
    title = value if field == "title" else "ordinary café 文 🚲"
    record_session_start(path, cwd=cwd, agent=agent, model="offline")
    append_title(path, title)
    before = path.read_bytes()
    process = subprocess.run(
        [sys.executable, "-c", "from js.cli import main; raise SystemExit(main(['--list', '--json']))"],
        cwd=tmp_path, capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8:strict"},
    )

    assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
    [record] = [json.loads(line) for line in process.stdout.decode("utf-8", errors="strict").splitlines()]
    assert record["title"] == title
    assert record["cwd"] == cwd
    assert record["agent"] == agent
    assert path.read_bytes() == before
