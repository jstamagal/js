"""Dynamic model capability lookup backed by models.dev data.

js keeps its own writable mirror of the models.dev catalog under platform data.
The bundled DB from the installed package is only the seed. On lookup we check
whether the active catalog is older than 8 hours; if so, we refresh it into
js's own DB and record when that happened. A forced refresh is available from
js CLI/REPL commands.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

import modelsdotdev
from modelsdotdev._internal import data as modelsdotdev_data
from modelsdotdev._internal import sync as modelsdotdev_sync

from . import codex_auth, paths, providers, settings as _settings

_CATALOG_MAX_AGE = timedelta(hours=8)
_STATUS_VERSION = 1
_LOCAL_PROBE_TIMEOUT_S = min(float(_settings.DEFAULT_FETCH_TIMEOUT_S), 3.0)
_LOCAL_PROBE_TRANSPORTS = {"ollama", "llama.cpp", "openai_compatible", "custom_openai"}
_OPENAI_PROBE_TRANSPORTS = {"openai_compatible", "custom_openai"}
_CONTEXT_WINDOW_KEYS = {
    "context_length",
    "context_window",
    "max_context_length",
    "max_context_window",
    "max_model_len",
    "max_model_length",
    "max_position_embeddings",
    "max_seq_len",
    "max_sequence_length",
    "max_input_tokens",
    "n_ctx",
    "num_ctx",
}


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
    _probe_local_context_window_cached.cache_clear()


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


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str):
        normalized = value.strip().replace("_", "")
        if normalized.isdigit():
            parsed = int(normalized)
            return parsed if parsed > 0 else None
    return None


def _context_key_matches(key: Any) -> bool:
    lowered = str(key).lower()
    return lowered in _CONTEXT_WINDOW_KEYS or any(
        lowered.endswith(f".{suffix}") for suffix in _CONTEXT_WINDOW_KEYS
    )


def _extract_context_window(payload: Any) -> int | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if _context_key_matches(key):
                parsed = _positive_int(value)
                if parsed is not None:
                    return parsed
        for value in payload.values():
            parsed = _extract_context_window(value)
            if parsed is not None:
                return parsed
    elif isinstance(payload, list):
        for item in payload:
            parsed = _extract_context_window(item)
            if parsed is not None:
                return parsed
    return None


def _join_url(base_url: str, suffix: str) -> str:
    return f"{base_url.rstrip('/')}/{suffix.lstrip('/')}"


def _server_root_url(base_url: str) -> str:
    split = urlsplit(base_url.rstrip("/"))
    path = split.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


def _request_json(method: str, url: str, *, json_body: dict[str, Any] | None = None) -> Any:
    with httpx.Client(timeout=_LOCAL_PROBE_TIMEOUT_S) as client:
        response = client.request(method, url, json=json_body)
        response.raise_for_status()
        return response.json()


def _models_payload_for_model(payload: Any, model_id: str) -> Any:
    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return payload
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == model_id:
            return entry
    return entries[0] if len(entries) == 1 else None


def _probe_openai_context_window(base_url: str, model_id: str) -> int | None:
    payload = _request_json("GET", _join_url(base_url, "models"))
    parsed = _extract_context_window(_models_payload_for_model(payload, model_id))
    if parsed is not None:
        return parsed

    model_path = quote(model_id, safe="")
    payload = _request_json("GET", _join_url(base_url, f"models/{model_path}"))
    return _extract_context_window(payload)


def _probe_ollama_context_window(base_url: str, model_id: str) -> int | None:
    payload = _request_json(
        "POST",
        _join_url(_server_root_url(base_url), "api/show"),
        json_body={"model": model_id},
    )
    return _extract_context_window(payload)


def _probe_llamacpp_context_window(base_url: str, model_id: str) -> int | None:
    url = _join_url(_server_root_url(base_url), f"props?model={quote(model_id, safe='')}")
    payload = _request_json("GET", url)
    return _extract_context_window(payload)


@lru_cache(maxsize=256)
def _probe_local_context_window_cached(probe_kind: str, base_url: str, model_id: str) -> int | None:
    try:
        if probe_kind == "ollama":
            return _probe_ollama_context_window(base_url, model_id)
        if probe_kind == "llama.cpp":
            return _probe_llamacpp_context_window(base_url, model_id)
        if probe_kind == "openai":
            return _probe_openai_context_window(base_url, model_id)
    except Exception:  # noqa: BLE001 - local metadata probes are strictly best-effort
        return None
    return None


def probe_local_context_window(
    model_id: str,
    provider_id: str | None = None,
    *,
    base_url: str | None = None,
) -> int | None:
    """Best-effort context-window probe for local model servers."""
    model_id, provider_id = _normalize_request(model_id, provider_id)
    provider = providers.get_provider(provider_id)
    if provider is None or provider.transport not in _LOCAL_PROBE_TRANSPORTS:
        return None
    resolved_base_url = providers.provider_base_url(provider, base_url)
    if not resolved_base_url:
        return None
    probe_kind = "openai" if provider.transport in _OPENAI_PROBE_TRANSPORTS else provider.transport
    return _probe_local_context_window_cached(probe_kind, resolved_base_url.rstrip("/"), model_id)


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
        print("*** updating models.dev cache...", file=sys.stderr)
        try:
            return refresh_catalog(force=True)
        except Exception as exc:
            print(f"*** warning: models.dev cache refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            if current is not None:
                _activate_database(current.db_path)
                return current
            raise
    if catalog_is_stale(current):
        print("*** updating models.dev cache...", file=sys.stderr)
        try:
            return refresh_catalog(force=True)
        except Exception as exc:
            print(f"*** warning: models.dev cache refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            if current is not None:
                _activate_database(current.db_path)
                return current
            raise
    return current


def _is_wrapper_request(catalog_id: str, request: str) -> bool:
    """True when ``request`` is ``catalog_id`` plus a wrapper suffix, e.g. the
    catalog id ``deepseek-v4-pro`` against the request ``deepseek-v4-pro:cloud``.

    Requires an exact prefix match followed by a boundary that is not itself
    part of a bare model slug (letters, digits, ``-`` all continue one), so a
    genuinely distinct sibling model — ``gpt-5-mini-2026`` against the catalog
    id ``gpt-5`` — is rejected: ``-`` continues the slug, it isn't a wrapper
    marker like ``:``.
    """
    if not request.startswith(catalog_id):
        return False
    tail = request[len(catalog_id):]
    return not tail or not (tail[0].isalnum() or tail[0] == "-")


@lru_cache(maxsize=512)
def lookup_limits(model_id: str, provider_id: str | None = None) -> ModelLimits | None:
    """Return dynamic limits for ``model_id`` using models.dev metadata.

    We first try exact provider+model matches using the active js provider mapped
    to its underlying models.dev provider id. If that misses, we scan the
    catalog: exact model-id matches first, then a wrapper-prefix match (see
    ``_is_wrapper_request``) so custom wrappers like ``deepseek-v4-pro:cloud``
    can still inherit the limits of the underlying ``deepseek-v4-pro`` row,
    without a shorter catalog id bleeding into an unrelated longer sibling.
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
    pattern_matches = [row for row in _all_models() if _is_wrapper_request(row.model_id.lower(), request)]
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


def _cached_server_metadata(model_id: str, provider_id: str | None):
    model_id, provider_id = _normalize_request(model_id, provider_id)
    if provider_id is None:
        return None, model_id, None
    provider_id = providers.normalize_provider_id(provider_id) or provider_id
    try:
        from . import logins

        provider_metadata = logins.load_model_cache_metadata().get(provider_id, {})
    except Exception:  # noqa: BLE001 - cache lookup must never break model resolution
        return None, model_id, provider_id
    return provider_metadata.get(model_id), model_id, provider_id


def cached_server_limits(model_id: str, provider_id: str | None = None) -> ModelLimits | None:
    metadata, normalized_model_id, normalized_provider_id = _cached_server_metadata(model_id, provider_id)
    if metadata is None:
        return None
    return ModelLimits(
        provider_id=normalized_provider_id,
        model_id=normalized_model_id,
        context_window=metadata.context_window,
        max_output_tokens=metadata.max_output_tokens,
        max_input_tokens=metadata.max_input_tokens,
    )


def cached_server_context_ceiling(model_id: str, provider_id: str | None = None) -> int | None:
    metadata, _normalized_model_id, _normalized_provider_id = _cached_server_metadata(model_id, provider_id)
    return None if metadata is None else metadata.training_context_window


def context_window(model_id: str, provider_id: str | None = None) -> int | None:
    ensure_fresh_catalog()
    limits = lookup_limits(model_id, provider_id)
    return None if limits is None else limits.context_window


def max_output_tokens(model_id: str, provider_id: str | None = None) -> int | None:
    ensure_fresh_catalog()
    limits = lookup_limits(model_id, provider_id)
    return None if limits is None else limits.max_output_tokens
