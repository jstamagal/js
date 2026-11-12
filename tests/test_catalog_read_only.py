"""Catalog inspections never create a missing SQLite database."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from js import model_metadata


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def status(path, when=NOW):
    return model_metadata.CatalogStatus(path, when, None, None, None, None)


def make_db(path, release_table=True):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT, value TEXT)")
        connection.execute("INSERT INTO metadata VALUES ('source', 'test')")
        if release_table:
            connection.execute("CREATE TABLE model_release_dates(value TEXT)")
        connection.commit()


def test_stale_check_leaves_missing_database_absent(tmp_path):
    path = tmp_path / "missing.db"
    assert model_metadata.catalog_is_stale(status(path), now=NOW)
    assert not path.exists()


@pytest.mark.parametrize("metadata", [False, True])
def test_disappearing_database_is_never_recreated(tmp_path, monkeypatch, metadata):
    path = tmp_path / "deleted.db"
    make_db(path)
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        path.unlink()
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(model_metadata.sqlite3, "connect", connect)
    if metadata:
        assert model_metadata._read_db_metadata(path) is None
    else:
        assert model_metadata.catalog_is_stale(status(path), now=NOW)
    assert not path.exists()


@pytest.mark.parametrize("name", ["ordinary.db", "special?#% space.db"])
def test_read_only_catalog_retains_valid_and_stale_behavior(tmp_path, name):
    path = tmp_path / name
    make_db(path)
    assert not model_metadata.catalog_is_stale(status(path), now=NOW)
    assert model_metadata._read_db_metadata(path) == {"source": "test"}
    assert model_metadata.catalog_is_stale(status(path, NOW - timedelta(days=365)), now=NOW)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE model_release_dates")
    assert model_metadata.catalog_is_stale(status(path), now=NOW)


def test_failed_metadata_query_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    path.touch()
    real_connect = sqlite3.connect
    closed = []

    class Connection:
        def __init__(self, *args, **kwargs):
            self.actual = real_connect(*args, **kwargs)

        def execute(self, *args):
            return self.actual.execute(*args)

        def close(self):
            self.actual.close()
            closed.append(True)

    monkeypatch.setattr(model_metadata.sqlite3, "connect", Connection)
    assert model_metadata._read_db_metadata(path) is None
    assert closed == [True]
