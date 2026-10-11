"""The session catalog and search index under ~/.js/cache: kept in step as
sessions are written, caught up from the files on open, rebuilt when missing,
and ranked with BM25 over what the operator and the model wrote. Every test
runs in the tmp HOME the conftest installs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from js import memory as M
from js import session_index, session_store, session_text
from js import session_query as Q
from js.session_catalog import append_title, record_session_start

STAMP = M.stamp_for("deepseek-v4-flash", "deepseek", "high")


def _session(cwd: Path, *, agent: str = "defaultagent", mode: str = "repl") -> Path:
    cwd.mkdir(parents=True, exist_ok=True)
    path = session_store.reserve(session_store.folder_for(cwd))
    record_session_start(path, cwd=cwd, agent=agent, model="deepseek-v4-flash", mode=mode)
    return path


def _say(path: Path, user: str, reply: str) -> None:
    M.append_message(path, {"role": "user", "content": user})
    M.append_message(path, {"role": "assistant", "content": reply}, STAMP)


def _call(path: Path, label: str, command: str, output: str, call_id: str = "c1") -> None:
    M.append_message(path, {"role": "assistant", "content": label, "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": "shell", "arguments": json.dumps({"command": command})}}]}, STAMP)
    M.append_message(path, {"role": "tool", "tool_call_id": call_id, "content": f"exit=0\n{output}"})


def _by_path(entries: list[dict]) -> dict[str, dict]:
    return {entry["path"]: entry for entry in entries}


def _key(path: Path) -> str:
    return str(path.resolve())


def _search(text: str) -> dict[str, float]:
    query = Q.parse_query(text, now=0, home="/", cwd="/")
    return session_index.search(Q.fts_expression(query))


def test_every_write_keeps_the_catalog_entry_current_without_a_reparse(tmp_path, monkeypatch):
    path = _session(tmp_path / "proj")
    _say(path, "first", "one")
    _call(path, "look", "ls", "out")
    _say(path, "second", "two")

    def no_parse(_path):
        raise AssertionError("catalog parsed a session the index already held")

    monkeypatch.setattr(session_text, "parse", no_parse)
    entry = _by_path(session_index.catalog())[_key(path)]
    assert (entry["turns"], entry["tool_calls"], entry["agent"], entry["mode"]) == (2, 1, "defaultagent", "repl")
    assert entry["cwd"] == str((tmp_path / "proj").resolve())
    assert entry["replied"] is True
    assert entry["models"] == ["deepseek-v4-flash"]


def test_a_file_written_behind_the_index_is_caught_up_on_open(tmp_path):
    path = _session(tmp_path / "proj")
    _say(path, "first", "one")
    with path.open("a", encoding="utf-8") as stream:
        for role, text in (("user", "second"), ("assistant", "two")):
            stream.write(json.dumps({"kind": "message", "version": 1, "ts": 1.0,
                                     "message": {"role": role, "content": text}}) + "\n")
    assert _by_path(session_index.catalog())[_key(path)]["turns"] == 2


def test_a_missing_index_is_rebuilt_from_the_sessions(tmp_path):
    first = _session(tmp_path / "a")
    _say(first, "hello niri", "hi")
    second = _session(tmp_path / "b")
    _say(second, "other", "reply")
    session_index.db_path().unlink()
    assert set(_by_path(session_index.catalog())) == {_key(first), _key(second)}
    assert set(_search("niri")) == {_key(first)}


def test_an_unreadable_index_is_rebuilt(tmp_path):
    path = _session(tmp_path / "a")
    _say(path, "hello", "hi")
    session_index.db_path().write_bytes(b"not a database at all" * 100)
    assert set(_by_path(session_index.catalog())) == {_key(path)}


def test_a_wiped_or_deleted_session_leaves_the_catalog(tmp_path):
    wiped = _session(tmp_path / "a")
    _say(wiped, "hello", "hi")
    deleted = _session(tmp_path / "b")
    _say(deleted, "bye", "ok")
    M.wipe(wiped)
    deleted.unlink()
    assert _by_path(session_index.catalog()) == {}


def test_subagent_runs_and_titles_are_catalogued(tmp_path):
    parent = _session(tmp_path / "proj")
    _say(parent, "fan out", "done")
    child = session_store.reserve(session_store.subagent_folder(parent), session_store.task_name)
    record_session_start(child, cwd=tmp_path / "proj", agent="worker", mode="subagent", parent=parent)
    append_title(parent, "the fan-out")
    entries = _by_path(session_index.catalog())
    assert entries[_key(child)]["mode"] == "subagent"
    assert entries[_key(child)]["parent"] == _key(parent)
    assert entries[_key(parent)]["title"] == "the fan-out"


def test_words_rank_by_bm25_over_operator_model_and_tool_label_text_never_tool_output(tmp_path):
    operator = _session(tmp_path / "a")
    _say(operator, "the motherboard died, the motherboard is dead, motherboard gone", "sorry")
    model = _session(tmp_path / "b")
    _say(model, "what broke", "probably the motherboard, and a lot of other words here too")
    label = _session(tmp_path / "c")
    _say(label, "check it", "ok")
    _call(label, "sniff the motherboard sensors", "sensors", "nothing")
    output = _session(tmp_path / "d")
    _say(output, "run it", "ok")
    _call(output, "", "dmesg", "motherboard firmware bug")
    scores = _search("motherboard")
    assert set(scores) == {_key(operator), _key(model), _key(label)}
    assert min(scores, key=scores.get) == _key(operator)


def test_the_command_line_of_a_call_is_searchable(tmp_path):
    path = _session(tmp_path / "a")
    _say(path, "temps", "ok")
    _call(path, "", "nvidia-smi --query", "70C")
    assert set(_search("nvidia")) == {_key(path)}


def test_every_word_is_required_and_a_word_matches_as_a_prefix(tmp_path):
    both = _session(tmp_path / "a")
    _say(both, "niri on the motherboard", "ok")
    one = _session(tmp_path / "b")
    _say(one, "niri only", "ok")
    assert set(_search("niri mother")) == {_key(both)}


def test_each_hit_carries_its_best_matching_row(tmp_path):
    path = _session(tmp_path / "a")
    _say(path, "hello there", "general words")
    _say(path, "the niri compositor on the motherboard", "right")
    expression = Q.fts_expression(Q.parse_query("niri motherboard", now=0, home="/", cwd="/"))
    scores = session_index.search(expression)
    hit = session_index.matching_lines(expression, scores)[_key(path)]
    assert (hit.number, hit.who) == (3, session_text.USER)
    assert [hit.line[start:end].lower() for start, end in hit.spans] == ["niri", "motherboard"]


@pytest.mark.parametrize("expression", ['"unbalanced', "AND AND"])
def test_a_malformed_expression_finds_nothing(tmp_path, expression):
    path = _session(tmp_path / "a")
    _say(path, "hello", "hi")
    assert session_index.search(expression) == {}


def test_rolled_back_rows_leave_the_search_text(tmp_path):
    path = _session(tmp_path / "a")
    M.persist_messages(path, [{"role": "user", "content": "zebra question"},
                              {"role": "assistant", "content": "zebra answer"}], STAMP)
    M.persist_messages(path, [{"role": "user", "content": "giraffe question"}], STAMP)
    assert set(_search("giraffe")) == {_key(path)}
    assert _search("zebra") == {}


# --- recovery must always re-probe (neema #958, titus #986, kito #964) --------

import fcntl  # noqa: E402
import sqlite3  # noqa: E402
from unittest.mock import patch  # noqa: E402


def test_recover_keeps_a_healthy_file_even_when_its_identity_matches_the_failed_one(tmp_path):
    # A freed inode number is reused, so (dev, ino) equality does not prove
    # this is the file that failed. Under the lock the file is probed again.
    path = tmp_path / "index.sqlite"
    session_index._open(path).close()
    seen = session_index._identity(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO sessions(path,size,mtime_ns,info) VALUES ('SURVIVOR',0,0,'{}')")
    recovered = session_index._recover(path, seen)
    assert recovered.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    recovered.close()


def test_recover_still_rebuilds_a_file_that_is_corrupt_under_the_lock(tmp_path):
    path = tmp_path / "index.sqlite"
    path.write_bytes(b"not a database" * 64)
    recovered = session_index._recover(path)
    assert recovered.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    recovered.close()


def test_recover_rebuilds_a_file_whose_table_pages_are_damaged(tmp_path):
    # `_open` counts rows through the small UNIQUE(path) index, so a damaged
    # page of the sessions table passes it while every catalog walk fails.
    path = tmp_path / "index.sqlite"
    session_index._open(path).close()
    connection = sqlite3.connect(path)
    connection.executemany("INSERT INTO sessions(path,size,mtime_ns,info) VALUES (?,0,0,?)",
                           [(f"s{i}", "x" * 1500) for i in range(300)])
    connection.commit()
    connection.execute("PRAGMA journal_mode = DELETE").fetchall()
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    leaves = [row[0] for row in connection.execute(
        "SELECT pageno FROM dbstat WHERE name = 'sessions' AND pagetype = 'leaf' ORDER BY pageno")]
    connection.close()
    data = bytearray(path.read_bytes())
    page = leaves[len(leaves) // 2]
    data[(page - 1) * page_size:page * page_size] = b"\xff" * page_size
    path.write_bytes(bytes(data))
    session_index._open(path).close()
    recovered = session_index._recover(path)
    try:
        assert recovered.execute("SELECT path FROM sessions").fetchall() == []
    finally:
        recovered.close()


def test_a_failed_rebuild_lock_closes_its_stream(tmp_path):
    lock = session_index._RebuildLock(tmp_path / "index.sqlite")
    opened = []
    real_open = open

    def spy(*args, **kwargs):
        stream = real_open(*args, **kwargs)
        opened.append(stream)
        return stream

    with patch("builtins.open", spy), patch.object(fcntl, "flock", side_effect=PermissionError("no")):
        with pytest.raises(PermissionError):
            lock.__enter__()
    assert opened and all(stream.closed for stream in opened)


class _Corrupt(sqlite3.DatabaseError):
    sqlite_errorcode = sqlite3.SQLITE_CORRUPT


class _Busy(sqlite3.OperationalError):
    sqlite_errorcode = sqlite3.SQLITE_BUSY


def test_corrupt_mid_walk_after_a_cached_connection_recovers_once(tmp_path):
    path = tmp_path / "sessions.sqlite"
    calls = []

    def fail_once(root):
        calls.append(1)
        if len(calls) == 1:
            raise _Corrupt("injected corrupt mid-walk")
        return []

    with patch.object(session_index, "db_path", return_value=path), \
            patch.object(session_index, "_catalog", side_effect=fail_once):
        session_index._connection()
        assert session_index.catalog(tmp_path) == []
        session_index._drop_current()
    assert len(calls) == 2


def test_a_busy_catalog_is_retried_and_deletes_nothing(tmp_path):
    calls = []

    def busy_twice(root):
        calls.append(1)
        if len(calls) < 3:
            raise _Busy("database is locked")
        return ["ok"]

    with patch.object(session_index, "_catalog", side_effect=busy_twice), \
            patch.object(session_index, "_remove", side_effect=AssertionError("deleted")):
        assert session_index.catalog(tmp_path) == ["ok"]
    assert len(calls) == 3


def test_a_catalog_busy_three_times_raises_and_deletes_nothing(tmp_path):
    with patch.object(session_index, "_catalog", side_effect=_Busy("database is locked")), \
            patch.object(session_index, "_remove", side_effect=AssertionError("deleted")):
        with pytest.raises(sqlite3.OperationalError):
            session_index.catalog(tmp_path)
