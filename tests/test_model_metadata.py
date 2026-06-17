from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from js import model_metadata


class _FakeModel:
    def __init__(self, provider_id: str, model_id: str, context: int | None, output: int | None, input_: int | None = None):
        self.provider_id = provider_id
        self.id = model_id
        self.limits = SimpleNamespace(context=context, output=output, input=input_)


def _write_metadata_db(path: Path, *, generated_at: str, source: str = "https://models.dev/api.json") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        [
            ("source", source),
            ("generated_at", generated_at),
            ("provider_count", "140"),
            ("model_count", "5142"),
        ],
    )
    conn.commit()
    conn.close()


def test_lookup_limits_prefers_exact_provider_match(monkeypatch):
    monkeypatch.setattr(model_metadata, "_all_models", lambda: ())
    model_metadata.lookup_limits.cache_clear()

    def fake_get_model_by_id(model_id: str):
        if model_id == "deepseek:deepseek-v4-flash":
            return _FakeModel("deepseek", "deepseek-v4-flash", 1_000_000, 384_000)
        return None

    monkeypatch.setattr(model_metadata.modelsdotdev, "get_model_by_id", fake_get_model_by_id)

    limits = model_metadata.lookup_limits("deepseek-v4-flash", "deepseek")

    assert limits is not None
    assert limits.provider_id == "deepseek"
    assert limits.context_window == 1_000_000
    assert limits.max_output_tokens == 384_000


def test_lookup_limits_pattern_matches_unknown_provider_wrappers(monkeypatch):
    rows = (
        model_metadata._ModelRow("deepseek", "deepseek-v4-pro", 1_000_000, 384_000, None),
        model_metadata._ModelRow("openai", "gpt-5.5", 1_050_000, 128_000, 922_000),
    )
    monkeypatch.setattr(model_metadata, "_all_models", lambda: rows)
    monkeypatch.setattr(model_metadata.modelsdotdev, "get_model_by_id", lambda _model_id: None)
    model_metadata.lookup_limits.cache_clear()

    limits = model_metadata.lookup_limits("deepseek-v4-pro:cloud", "omp")

    assert limits is not None
    assert limits.provider_id == "deepseek"
    assert limits.model_id == "deepseek-v4-pro"
    assert limits.context_window == 1_000_000
    assert limits.max_output_tokens == 384_000


def test_ensure_fresh_catalog_refreshes_stale_bundle_and_writes_status(monkeypatch, tmp_path: Path):
    old_time = datetime.now(tz=UTC) - timedelta(days=5)
    new_time = datetime.now(tz=UTC)
    bundled = tmp_path / "bundled.sqlite"
    custom = tmp_path / "custom.sqlite"
    status_path = tmp_path / "status.json"
    _write_metadata_db(bundled, generated_at=old_time.isoformat())

    monkeypatch.delenv("MODELDOTDEV_DATABASE_PATH", raising=False)
    monkeypatch.setattr(model_metadata, "bundled_db_path", lambda: bundled)
    monkeypatch.setattr(model_metadata, "_custom_db_path", lambda: custom)
    monkeypatch.setattr(model_metadata, "_status_file_path", lambda: status_path)

    def fake_generate_database(*, output: Path, source: str = model_metadata.modelsdotdev_sync.API_URL):
        _write_metadata_db(output, generated_at=new_time.isoformat(), source=source)
        return 140, 5142

    monkeypatch.setattr(model_metadata.modelsdotdev_sync, "generate_database", fake_generate_database)
    model_metadata.lookup_limits.cache_clear()
    model_metadata._all_models.cache_clear()

    status = model_metadata.ensure_fresh_catalog()

    assert status is not None
    assert status.db_path == custom
    assert status.generated_at == new_time
    assert status.refreshed_at is not None
    assert os.environ["MODELDOTDEV_DATABASE_PATH"] == str(custom)
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["db_path"] == str(custom)
    assert payload["generated_at"] == new_time.isoformat()
    assert payload["refreshed_at"] is not None


def test_ensure_fresh_catalog_keeps_recent_custom_db_without_refresh(monkeypatch, tmp_path: Path):
    recent = datetime.now(tz=UTC) - timedelta(hours=4)
    custom = tmp_path / "custom.sqlite"
    status_path = tmp_path / "status.json"
    _write_metadata_db(custom, generated_at=recent.isoformat())
    status_path.write_text(
        json.dumps(
            {
                "version": 1,
                "db_path": str(custom),
                "generated_at": recent.isoformat(),
                "refreshed_at": recent.isoformat(),
                "source": "https://models.dev/api.json",
                "provider_count": 140,
                "model_count": 5142,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("MODELDOTDEV_DATABASE_PATH", raising=False)
    monkeypatch.setattr(model_metadata, "_custom_db_path", lambda: custom)
    monkeypatch.setattr(model_metadata, "_status_file_path", lambda: status_path)
    monkeypatch.setattr(model_metadata.modelsdotdev_sync, "generate_database", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not refresh")))

    status = model_metadata.ensure_fresh_catalog()

    assert status is not None
    assert status.db_path == custom
    assert status.generated_at == recent
    assert os.environ["MODELDOTDEV_DATABASE_PATH"] == str(custom)
