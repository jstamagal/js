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
    conn.execute("CREATE TABLE model_release_dates (full_id TEXT PRIMARY KEY, release_date TEXT NOT NULL)")
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


def test_lookup_limits_pattern_match_rejects_sibling_but_keeps_wrapper(monkeypatch):
    """The bidirectional substring fallback let a shorter catalog id (gpt-5) bleed
    into a longer, genuinely-distinct sibling request (gpt-5-mini-2026) since '-'
    is not alnum. The fix requires the wrapper boundary to not be '-' either, so
    only a true wrapper suffix like ':cloud' still inherits the base model."""
    rows = (
        model_metadata._ModelRow("openai", "gpt-5", 400_000, 128_000, None),
        model_metadata._ModelRow("deepseek", "deepseek-v4-pro", 1_000_000, 384_000, None),
    )
    monkeypatch.setattr(model_metadata, "_all_models", lambda: rows)
    monkeypatch.setattr(model_metadata.modelsdotdev, "get_model_by_id", lambda _model_id: None)
    model_metadata.lookup_limits.cache_clear()

    assert model_metadata.lookup_limits("gpt-5-mini-2026", "openai") is None

    limits = model_metadata.lookup_limits("deepseek-v4-pro:cloud", "omp")
    assert limits is not None
    assert limits.model_id == "deepseek-v4-pro"
    assert limits.context_window == 1_000_000
    assert limits.max_output_tokens == 384_000


def test_ensure_fresh_catalog_refreshes_stale_bundle_and_writes_status(monkeypatch, tmp_path: Path, capsys):
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

    monkeypatch.setattr(model_metadata, "_generate_database", fake_generate_database)
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
    assert "*** updating models.dev cache..." in capsys.readouterr().err


def test_ensure_fresh_catalog_keeps_recent_custom_db_without_refresh(monkeypatch, tmp_path: Path, capsys):
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
    monkeypatch.setattr(model_metadata, "_generate_database", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not refresh")))

    status = model_metadata.ensure_fresh_catalog()

    assert status is not None
    assert status.db_path == custom
    assert status.generated_at == recent
    assert os.environ["MODELDOTDEV_DATABASE_PATH"] == str(custom)
    assert capsys.readouterr().err == ""


def test_ensure_fresh_catalog_warns_and_keeps_current_on_refresh_failure(monkeypatch, tmp_path: Path, capsys):
    old_time = datetime.now(tz=UTC) - timedelta(days=5)
    custom = tmp_path / "custom.sqlite"
    status_path = tmp_path / "status.json"
    _write_metadata_db(custom, generated_at=old_time.isoformat())
    status_path.write_text(
        json.dumps(
            {
                "version": 1,
                "db_path": str(custom),
                "generated_at": old_time.isoformat(),
                "refreshed_at": old_time.isoformat(),
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

    def fail_refresh(**_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(model_metadata, "_generate_database", fail_refresh)

    status = model_metadata.ensure_fresh_catalog()

    assert status is not None
    assert status.db_path == custom
    err = capsys.readouterr().err
    assert "*** updating models.dev cache..." in err
    assert "*** warning: models.dev cache refresh failed: RuntimeError: offline" in err


def test_catalog_refresh_retains_release_dates(monkeypatch, tmp_path):
    payload = {
        "maker": {
            "id": "maker", "name": "Maker", "npm": "@ai-sdk/openai-compatible",
            "doc": "https://example.test", "env": [],
            "models": {
                "model-v2": {
                    "id": "model-v2", "name": "Model V2", "family": "model",
                    "release_date": "2026-05-01", "attachment": False,
                    "reasoning": True, "tool_call": True, "open_weights": False,
                    "limit": {"context": 1000000, "output": 32000},
                    "modalities": {"input": ["text"], "output": ["text"]},
                },
            },
        },
    }
    path = tmp_path / "catalog.sqlite"
    monkeypatch.setattr(model_metadata.modelsdotdev_sync, "_load_providers", lambda source: payload)
    monkeypatch.setattr(model_metadata, "_custom_db_path", lambda: path)
    model_metadata._generate_database(output=path)
    assert model_metadata._release_dates() == {"maker:model-v2": "2026-05-01"}
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT limit_context FROM models").fetchone() == (1000000,)


def test_routed_latest_uses_family_release_date_and_preserves_variants(monkeypatch):
    from js.model_metadata import _ModelRow

    rows = (
        _ModelRow("openai", "gpt-5.6-sol", 1050000, 128000, None, "gpt-sol", "2026-07-09"),
        _ModelRow("upstage", "solar-pro4", 524288, 32000, None, "solar-pro", "2026-09-01"),
        _ModelRow("anthropic", "claude-opus-5", 1000000, 128000, None, "claude-opus", "2026-07-24"),
        _ModelRow("router", "claude-opus-5-fast", 1000000, 128000, None, "claude-opus", "2026-08-01"),
        _ModelRow("xai", "grok-4-0709", 256000, 32000, None, "grok", "2025-07-09"),
        _ModelRow("xai", "grok-4.6", 500000, 128000, None, "grok", "2026-08-12"),
        _ModelRow("minimax", "minimax-m27", 198000, 32000, None, "minimax", "2026-03-18"),
        _ModelRow("minimax", "minimax-m3", 1048576, 128000, None, "minimax", "2026-06-01"),
        _ModelRow("alibaba", "qwen3.8-max", 1000000, 32000, None, "qwen", "2026-08-03"),
        _ModelRow("alibaba", "qwen3.8-flash", 1000000, 32000, None, "qwen", "2026-08-26"),
    )
    monkeypatch.setattr(model_metadata, "_all_models", lambda: rows)
    monkeypatch.setattr(model_metadata.modelsdotdev, "get_model_by_id", lambda _: None)
    model_metadata.lookup_limits.cache_clear()
    expected = {
        "cpa/sol-latest-high": "gpt-5.6-sol",
        "claude-opus-latest-max": "claude-opus-5",
        "grok-latest-off": "grok-4.6",
        "minimax-latest": "minimax-m3",
        "qwen-max-latest": "qwen3.8-max",
        "qwen-flash-latest": "qwen3.8-flash",
        "nim/gpt-5.6-sol": "gpt-5.6-sol",
        "grok-4-0709": "grok-4-0709",
    }
    for request, target in expected.items():
        result = model_metadata.lookup_limits(request, "cpa")
        assert result is not None, request
        assert result.model_id == target


def test_routed_catalog_prefers_issuer_limits(monkeypatch):
    rows = (
        model_metadata._ModelRow("router", "gpt-5.6-sol", 128000, 32000, None, "gpt-sol", "2026-07-09"),
        model_metadata._ModelRow("openai", "gpt-5.6-sol", 1050000, 128000, None, "gpt-sol", "2026-07-09"),
    )
    monkeypatch.setattr(model_metadata, "_all_models", lambda: rows)
    monkeypatch.setattr(model_metadata.modelsdotdev, "get_model_by_id", lambda _: None)
    model_metadata.lookup_limits.cache_clear()
    result = model_metadata.lookup_limits("omni/gpt-5.6-sol", "cpa")
    assert result.provider_id == "openai"
    assert result.context_window == 1050000
