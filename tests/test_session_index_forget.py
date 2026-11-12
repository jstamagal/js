"""Failed forget transactions leave the cached index usable and unchanged."""

import sqlite3

import pytest

from js import session_index


@pytest.mark.parametrize("statement", ["DELETE FROM docs", "DELETE FROM sessions", "COMMIT"])
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, KeyboardInterrupt])
def test_failed_forget_rolls_back_on_same_connection(tmp_path, monkeypatch, statement, error_type):
    path = tmp_path / "session.jsonl"
    session_index.store(path, {"title": "keep"}, [(1, "user", None, "searchable text")], size=1, mtime_ns=1)
    connection = session_index._connection()
    original_sessions = connection.execute("SELECT * FROM sessions").fetchall()
    original_docs = connection.execute("SELECT rowid, * FROM docs").fetchall()
    failure = error_type("injected forget failure")

    class FailingConnection:
        def execute(self, sql, *args):
            if sql.startswith(statement):
                raise failure
            return connection.execute(sql, *args)

    with monkeypatch.context() as patched:
        patched.setattr(session_index, "_connection", lambda: FailingConnection())
        try:
            with pytest.raises(error_type) as caught:
                session_index.forget(path)
            assert caught.value is failure
            assert not connection.in_transaction
            assert connection.execute("SELECT * FROM sessions").fetchall() == original_sessions
            assert connection.execute("SELECT rowid, * FROM docs").fetchall() == original_docs
        finally:
            if connection.in_transaction:
                connection.rollback()

    assert session_index._connection() is connection
    neighbor = tmp_path / "neighbor.jsonl"
    session_index.store(neighbor, {"title": "next"}, [(1, "user", None, "neighbor")], size=2, mtime_ns=2)
    session_index.forget(path)
    assert not connection.in_transaction
    assert connection.execute("SELECT path FROM sessions").fetchall() == [(str(neighbor.resolve()),)]
    assert connection.execute("SELECT text FROM docs").fetchall() == [("neighbor",)]
