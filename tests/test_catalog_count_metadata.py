"""Corrupt catalog count metadata is stale rather than a startup exception."""

import sqlite3
from pathlib import Path

import pytest

from js import model_metadata


def catalog(path: Path, counts: dict[str, str]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany("INSERT INTO metadata VALUES (?, ?)", {
            "source": "test",
            **counts,
        }.items())


@pytest.mark.parametrize("key", ["provider_count", "model_count"])
@pytest.mark.parametrize("value", ["broken", "NaN", "Infinity", "1.5"])
@pytest.mark.parametrize("custom", [False, True])
def test_corrupt_catalog_counts_are_stale(tmp_path, monkeypatch, key, value, custom):
    path = tmp_path / "catalog.db"
    catalog(path, {"provider_count": "140", "model_count": "9000", key: value})
    monkeypatch.setattr(model_metadata, "_custom_db_path", lambda: path if custom else tmp_path / "absent.db")
    monkeypatch.setattr(model_metadata, "bundled_db_path", lambda: path)
    monkeypatch.setattr(model_metadata, "_status_file_path", lambda: tmp_path / "absent.json")

    status = model_metadata.current_catalog_status()
    assert status is None
    assert model_metadata.catalog_is_stale(status)


@pytest.mark.parametrize("counts,expected", [
    ({"provider_count": "140", "model_count": "9000"}, (140, 9000)),
    ({"provider_count": " 140 ", "model_count": " 9000 "}, (140, 9000)),
    ({"provider_count": "", "model_count": ""}, (None, None)),
    ({}, (None, None)),
])
def test_valid_or_optional_counts_keep_catalog_status(tmp_path, counts, expected):
    path = tmp_path / "catalog.db"
    catalog(path, counts)
    status = model_metadata._status_from_db(path)
    assert status is not None
    assert (status.provider_count, status.model_count) == expected
    assert status.db_path == path
