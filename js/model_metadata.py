"""Dynamic model capability lookup backed by models.dev data.

js keeps its own writable mirror of the models.dev catalog under platform data.
The bundled DB from the installed package is only the seed. On lookup we check
whether the active catalog is older than 72 hours; if so, we refresh it into
js's own DB and record when that happened. A forced refresh is available from
js CLI/REPL commands.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import modelsdotdev
from modelsdotdev._internal import data as modelsdotdev_data
from modelsdotdev._internal import sync as modelsdotdev_sync

from . import codex_auth, paths, providers

_CATALOG_MAX_AGE = timedelta(hours=72)
_STATUS_VERSION = 1


@dataclass(frozen=True)
class ModelLimits:
    provider_id: str | None
    model_id: str
    context_window: int | None
    max_output_tokens: int | None
    max_input_tokens: int | None


@dataclass(frozen=True)
class CatalogStatus:
    db_path: Path
    generated_at: datetime | None
    refreshed_at: datetime | None
    source: str | None
    provider_count: int | None
    model_count: int | None

    @property
    def checked_at(self) -> datetime | None:
        return self.refreshed_at or self.generated_at


@dataclass(frozen=True)
class _ModelRow:
    provider_id: str
    model_id: str
    context_window: int | None
    max_output_tokens: int | None
    max_input_tokens: int | None


def _clear_caches() -> None:
    _all_models.cache_clear()
    lookup_limits.cache_clear()


@lru_cache(maxsize=1)
def _all_models() -> tuple[_ModelRow, ...]:
    rows: list[_ModelRow] = []
    for model in modelsdotdev.iter_models():
        limits = getattr(model, "limits", None)
        rows.append(
            _ModelRow(
                provider_id=model.provider_id,
                model_id=model.id,
                context_window=None if limits is None else limits.context,
                max_output_tokens=None if limits is None else limits.output,
                max_input_tokens=None if limits is None else limits.input,
            )
        )
    return tuple(rows)


def _provider_candidates(provider_id: str | None) -> tuple[str, ...]:
    candidates: list[str] = []
    normalized = providers.normalize_provider_id(provider_id) if provider_id else None
    if normalized:
        provider = providers.get_provider(normalized)
        if provider is not None and provider.effective_sdk_provider_id:
            candidates.append(provider.effective_sdk_provider_id)
        if codex_auth.is_codex_provider(normalized):
            candidates.append("openai")
        candidates.append(normalized)
    seen: set[str] = set()
    ordered: list[str] = []
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return tuple(ordered)


def _normalize_request(model_id: str, provider_id: str | None) -> tuple[str, str | None]:
    parsed_provider, parsed_model = providers.parse_model_prefix(model_id)
    if parsed_provider is not None and parsed_model is not None:
        return parsed_model, parsed_provider
    return model_id, provider_id


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

def _status_file_path() -> Path:
    return paths.model_catalog_status_path()


def _custom_db_path() -> Path:
    return paths.model_catalog_db_path()


def bundled_db_path() -> Path:
    return modelsdotdev_data.DB_PATH


def _activate_database(path: Path) -> None:
    os.environ[modelsdotdev_data.DATABASE_PATH_ENV] = str(path)


def _read_db_metadata(path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path)) as connection:
            rows = connection.execute("SELECT key, value FROM metadata").fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    return {str(key): str(value) for key, value in rows}


def _status_from_db(path: Path, *, refreshed_at: datetime | None = None) -> CatalogStatus | None:
    metadata = _read_db_metadata(path)
    if metadata is None:
        return None
    return CatalogStatus(
        db_path=path,
        generated_at=_parse_dt(metadata.get("generated_at")),
        refreshed_at=refreshed_at,
        source=metadata.get("source"),
        provider_count=int(metadata["provider_count"]) if metadata.get("provider_count") else None,
        model_count=int(metadata["model_count"]) if metadata.get("model_count") else None,
    )


def _read_status_file() -> CatalogStatus | None:
    path = _status_file_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("version") != _STATUS_VERSION:
        return None
    db_path_raw = data.get("db_path")
    if not isinstance(db_path_raw, str):
        return None
    db_path = Path(db_path_raw)
    if not db_path.is_file():
        return None
    return CatalogStatus(
        db_path=db_path,
        generated_at=_parse_dt(data.get("generated_at") if isinstance(data.get("generated_at"), str) else None),
        refreshed_at=_parse_dt(data.get("refreshed_at") if isinstance(data.get("refreshed_at"), str) else None),
        source=data.get("source") if isinstance(data.get("source"), str) else None,
        provider_count=int(data["provider_count"]) if isinstance(data.get("provider_count"), int) else None,
        model_count=int(data["model_count"]) if isinstance(data.get("model_count"), int) else None,
    )


def _write_status_file(status: CatalogStatus) -> None:
    path = _status_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": _STATUS_VERSION,
        "db_path": str(status.db_path),
        "generated_at": None if status.generated_at is None else status.generated_at.isoformat(),
        "refreshed_at": None if status.refreshed_at is None else status.refreshed_at.isoformat(),
        "source": status.source,
        "provider_count": status.provider_count,
        "model_count": status.model_count,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def current_catalog_status() -> CatalogStatus | None:
    custom = _custom_db_path()
    if custom.is_file():
        status = _read_status_file()
        if status is not None and status.db_path == custom:
            return status
        return _status_from_db(custom)
    return _status_from_db(bundled_db_path())


def catalog_is_stale(status: CatalogStatus | None, *, now: datetime | None = None) -> bool:
    if status is None:
        return True
    checked_at = status.checked_at
    if checked_at is None:
        return True
    current = datetime.now(tz=UTC) if now is None else now.astimezone(UTC)
    return current - checked_at >= _CATALOG_MAX_AGE


def refresh_catalog(*, force: bool = False) -> CatalogStatus:
    current = current_catalog_status()
    if not force and not catalog_is_stale(current):
        assert current is not None
        _activate_database(current.db_path)
        return current

    output = _custom_db_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    modelsdotdev_sync.generate_database(output=output)
    _activate_database(output)
    refreshed_at = datetime.now(tz=UTC)
    status = _status_from_db(output, refreshed_at=refreshed_at)
    if status is None:
        raise RuntimeError(f"refreshed models.dev catalog missing metadata: {output}")
    _write_status_file(status)
    _clear_caches()
    return status


def ensure_fresh_catalog(*, force: bool = False) -> CatalogStatus | None:
    current = current_catalog_status()
    if current is not None:
        _activate_database(current.db_path)
    if force:
        return refresh_catalog(force=True)
    if catalog_is_stale(current):
        try:
            return refresh_catalog(force=True)
        except Exception:
            if current is not None:
                _activate_database(current.db_path)
                return current
            raise
    return current


@lru_cache(maxsize=512)
def lookup_limits(model_id: str, provider_id: str | None = None) -> ModelLimits | None:
    """Return dynamic limits for ``model_id`` using models.dev metadata.

    We first try exact provider+model matches using the active js provider mapped
    to its underlying models.dev provider id. If that misses, we scan the
    catalog: exact model-id matches first, then a substring pattern match so
    custom wrappers like ``deepseek-v4-pro:cloud`` can still inherit the limits
    of the underlying ``deepseek-v4-pro`` row.
    """

    model_id, provider_id = _normalize_request(model_id, provider_id)
    candidates = _provider_candidates(provider_id)

    for candidate in candidates:
        model = modelsdotdev.get_model_by_id(f"{candidate}:{model_id}")
        if model is not None:
            limits = getattr(model, "limits", None)
            return ModelLimits(
                provider_id=model.provider_id,
                model_id=model.id,
                context_window=None if limits is None else limits.context,
                max_output_tokens=None if limits is None else limits.output,
                max_input_tokens=None if limits is None else limits.input,
            )

    exact_matches = [row for row in _all_models() if row.model_id == model_id]
    if exact_matches:
        for candidate in candidates:
            for row in exact_matches:
                if row.provider_id == candidate:
                    return ModelLimits(
                        provider_id=row.provider_id,
                        model_id=row.model_id,
                        context_window=row.context_window,
                        max_output_tokens=row.max_output_tokens,
                        max_input_tokens=row.max_input_tokens,
                    )
        row = exact_matches[0]
        return ModelLimits(
            provider_id=row.provider_id,
            model_id=row.model_id,
            context_window=row.context_window,
            max_output_tokens=row.max_output_tokens,
            max_input_tokens=row.max_input_tokens,
        )

    request = model_id.lower()
    pattern_matches = [
        row for row in _all_models()
        if row.model_id.lower() in request or request in row.model_id.lower()
    ]
    if not pattern_matches:
        return None

    def _rank(row: _ModelRow) -> tuple[int, int, int, str]:
        preferred = 1 if row.provider_id in candidates else 0
        provider_hint = 1 if row.provider_id.lower() in request else 0
        return (preferred, provider_hint, len(row.model_id), row.provider_id)

    row = sorted(pattern_matches, key=_rank, reverse=True)[0]
    return ModelLimits(
        provider_id=row.provider_id,
        model_id=row.model_id,
        context_window=row.context_window,
        max_output_tokens=row.max_output_tokens,
        max_input_tokens=row.max_input_tokens,
    )


def context_window(model_id: str, provider_id: str | None = None) -> int | None:
    ensure_fresh_catalog()
    limits = lookup_limits(model_id, provider_id)
    return None if limits is None else limits.context_window


def max_output_tokens(model_id: str, provider_id: str | None = None) -> int | None:
    ensure_fresh_catalog()
    limits = lookup_limits(model_id, provider_id)
    return None if limits is None else limits.max_output_tokens
