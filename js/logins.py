"""Multi-provider login state and cached model lists.

Logins and model caches live in the js platform config directory.  Provider ids
are user-facing js ids; the registry decides the SDK/API shape used at runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import ai
import httpx
import tomli_w
import tomllib

from . import paths, providers

_CONFIG_DIR_OVERRIDE: Path | None = None
_LOGINS_FILE = "logins.toml"
_CACHE_FILE = "models-cache.json"


class LoginsCorruptError(RuntimeError):
    """logins.toml exists but failed to parse.

    Raised only by the write path (save_login/remove_login): rewriting the
    file from a load that silently swallowed a parse error would truncate
    every other saved login down to whatever this one call knows about.
    load_logins() itself stays lenient (returns {}) for plain reads, since
    those run on every route/picker call and must never crash a turn.
    """


def set_config_dir(path: Path | None) -> None:
    """Override the directory used for logins/cache. Tests use this."""
    global _CONFIG_DIR_OVERRIDE
    _CONFIG_DIR_OVERRIDE = path


def _config_dir() -> Path:
    return _CONFIG_DIR_OVERRIDE or paths.login_store_dir()


def _logins_path() -> Path:
    return _config_dir() / _LOGINS_FILE


def _cache_path() -> Path:
    return _config_dir() / _CACHE_FILE


@dataclass
class Login:
    # User-facing saved provider name, e.g. "deepseek", "mimo", "my-proxy".
    provider_id: str
    # SDK/API shape used to talk to it. None means the provider registry decides.
    sdk_provider_id: str | None = None
    # Exact login shape selected by the operator, e.g. openai-responses vs
    # openai-completions. None means legacy stores use sdk_provider_id mapping.
    shape_provider_id: str | None = None
    provider_base_url: str | None = None
    provider_api_key: str | None = None
    provider_headers: dict[str, str] = field(default_factory=dict)
    # OpenAI Codex OAuth metadata.  provider_api_key stores the short-lived
    # access JWT so the existing provider tuple keeps working; these fields keep
    # the refreshable session private in logins.toml.
    codex_refresh_token: str | None = None
    codex_token_expires: float | None = None
    codex_account_id: str | None = None
    codex_email: str | None = None

    @property
    def effective_provider_id(self) -> str:
        if self.sdk_provider_id:
            return self.sdk_provider_id
        provider = providers.get_provider(self.provider_id)
        if provider is not None and provider.effective_sdk_provider_id:
            return provider.effective_sdk_provider_id
        return self.provider_id


@dataclass(frozen=True)
class ModelCacheMetadata:
    # Served context/window limit from the endpoint. This intentionally excludes
    # training-only hints such as n_ctx_train.
    context_window: int | None = None
    max_output_tokens: int | None = None
    max_input_tokens: int | None = None
    # Training/context ceiling hint. Useful to cap an external fallback, but not
    # a served hard limit by itself.
    training_context_window: int | None = None


_SERVED_CONTEXT_KEYS = {
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
_TRAINING_CONTEXT_KEYS = {"n_ctx_train", "training_context_window", "train_context_window"}
_MAX_OUTPUT_KEYS = {
    "max_output_tokens",
    "max_completion_tokens",
    "output_token_limit",
    "max_new_tokens",
    "max_tokens",
}
_MAX_INPUT_KEYS = {"max_input_tokens", "input_token_limit"}
_OPENAI_MODEL_METADATA_TRANSPORTS = {
    "deepseek",
    "openai",
    "openai_compatible",
    "custom_openai",
    "custom_responses",
    "ollama",
    "llama.cpp",
    "cliproxyapi",
}


def _ensure_config_dir() -> None:
    _config_dir().mkdir(parents=True, exist_ok=True)

def _write_logins_toml(data: dict[str, dict[str, Any]]) -> None:
    """Write logins.toml atomically: temp file in the same dir, then rename.

    The old O_TRUNC-in-place write left a window where a crash/Ctrl-C mid-dump
    truncated the real file; os.replace() is atomic on the same filesystem so
    readers only ever see the old file or the fully-written new one.
    """
    _ensure_config_dir()
    path = _logins_path()
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(data, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _refuse_if_corrupt() -> None:
    """Raise LoginsCorruptError if logins.toml exists but fails to parse.

    Called by save_login/remove_login before they load-then-rewrite, so a
    corrupt file is never silently replaced by whatever this one call knows.
    """
    path = _logins_path()
    if not path.exists():
        return
    try:
        with path.open("rb") as f:
            tomllib.load(f)
    except Exception as exc:
        raise LoginsCorruptError(
            f"{path} exists but failed to parse as TOML ({exc}); refusing to overwrite it. "
            "Fix or remove the file by hand, then retry."
        ) from exc


def load_logins() -> dict[str, Login]:
    """Return map provider_id -> Login.

    A malformed file degrades to {} here — this runs on every routing/picker
    call and must never crash a turn. The write path (save_login/
    remove_login) calls _refuse_if_corrupt() first instead, since silently
    treating a corrupt file as empty there would truncate every stored login.
    """
    if not _logins_path().exists():
        return {}
    try:
        with _logins_path().open("rb") as f:
            data = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return {}
    logins: dict[str, Login] = {}
    for provider_id, raw in data.items():
        if not isinstance(raw, dict):
            continue
        headers = raw.get("provider_headers") if isinstance(raw.get("provider_headers"), dict) else {}
        logins[providers.normalize_provider_id(provider_id) or provider_id] = Login(
            provider_id=providers.normalize_provider_id(raw.get("provider_id", provider_id)) or provider_id,
            sdk_provider_id=raw.get("sdk_provider_id") or None,
            shape_provider_id=raw.get("shape_provider_id") or None,
            provider_base_url=raw.get("provider_base_url") or None,
            provider_api_key=raw.get("provider_api_key") or None,
            provider_headers={str(k): str(v) for k, v in headers.items()},
            codex_refresh_token=raw.get("codex_refresh_token") or None,
            codex_token_expires=raw.get("codex_token_expires") or None,
            codex_account_id=raw.get("codex_account_id") or None,
            codex_email=raw.get("codex_email") or None,
        )
    return logins


def save_login(login: Login) -> None:
    """Add or update a provider login and persist.

    Raises LoginsCorruptError instead of proceeding if the existing file
    fails to parse (see _refuse_if_corrupt).
    """
    _ensure_config_dir()
    _refuse_if_corrupt()
    canonical_id = providers.normalize_provider_id(login.provider_id) or login.provider_id
    login = replace(login, provider_id=canonical_id)
    loaded = load_logins()
    loaded[canonical_id] = login
    data: dict[str, dict[str, Any]] = {}
    for lid, item in loaded.items():
        data[lid] = {k: v for k, v in asdict(item).items() if v not in (None, {}, [])}
    _write_logins_toml(data)


def _normalize_provider_id(provider_id: str) -> str:
    return providers.normalize_provider_id(provider_id) or provider_id


def remove_login(provider_id: str) -> bool:
    """Remove a provider login. Returns True if it existed.

    Raises LoginsCorruptError instead of proceeding if the existing file
    fails to parse (see _refuse_if_corrupt).
    """
    provider_id = _normalize_provider_id(provider_id)
    _refuse_if_corrupt()
    loaded = load_logins()
    existed = provider_id in loaded
    if existed:
        del loaded[provider_id]
        _ensure_config_dir()
        data: dict[str, dict[str, Any]] = {}
        for lid, item in loaded.items():
            data[lid] = {k: v for k, v in asdict(item).items() if v not in (None, {}, [])}
        _write_logins_toml(data)
    # Also drop its cached models.
    cache = load_model_cache()
    if provider_id in cache:
        del cache[provider_id]
        save_model_cache(cache)
    return existed


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


def _metadata_key_matches(key: Any, names: set[str]) -> bool:
    lowered = str(key).lower()
    return lowered in names or any(lowered.endswith(f".{name}") for name in names)


def _find_positive_int(payload: Any, names: set[str]) -> int | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if _metadata_key_matches(key, names):
                parsed = _positive_int(value)
                if parsed is not None:
                    return parsed
        for value in payload.values():
            parsed = _find_positive_int(value, names)
            if parsed is not None:
                return parsed
    elif isinstance(payload, list):
        for item in payload:
            parsed = _find_positive_int(item, names)
            if parsed is not None:
                return parsed
    return None


def _coerce_metadata(raw: Any) -> ModelCacheMetadata | None:
    if not isinstance(raw, dict):
        return None
    metadata = ModelCacheMetadata(
        context_window=_positive_int(raw.get("context_window")),
        max_output_tokens=_positive_int(raw.get("max_output_tokens")),
        max_input_tokens=_positive_int(raw.get("max_input_tokens")),
        training_context_window=_positive_int(raw.get("training_context_window")),
    )
    return metadata if any(asdict(metadata).values()) else None


def _metadata_payload(metadata: ModelCacheMetadata) -> dict[str, int]:
    return {k: v for k, v in asdict(metadata).items() if isinstance(v, int) and v > 0}


def _metadata_from_model_payload(entry: Any) -> ModelCacheMetadata | None:
    if not isinstance(entry, dict):
        return None
    metadata = ModelCacheMetadata(
        context_window=_find_positive_int(entry, _SERVED_CONTEXT_KEYS),
        max_output_tokens=_find_positive_int(entry, _MAX_OUTPUT_KEYS),
        max_input_tokens=_find_positive_int(entry, _MAX_INPUT_KEYS),
        training_context_window=_find_positive_int(entry, _TRAINING_CONTEXT_KEYS),
    )
    return metadata if any(asdict(metadata).values()) else None


def metadata_from_openai_models_payload(payload: Any) -> dict[str, ModelCacheMetadata]:
    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return {}
    out: dict[str, ModelCacheMetadata] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        metadata = _metadata_from_model_payload(entry)
        if metadata is not None:
            out[model_id] = metadata
    return out


def _coerce_model_cache_records(data: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        return {}
    records: dict[str, dict[str, Any]] = {}
    for provider_id, raw in data.items():
        if isinstance(raw, list):
            models = [str(m) for m in raw]
            metadata: dict[str, ModelCacheMetadata] = {}
        elif isinstance(raw, dict):
            models_raw = raw.get("models")
            if not isinstance(models_raw, list):
                continue
            models = [str(m) for m in models_raw]
            metadata = {}
            metadata_raw = raw.get("metadata")
            if isinstance(metadata_raw, dict):
                for model_id, item in metadata_raw.items():
                    parsed = _coerce_metadata(item)
                    if parsed is not None:
                        metadata[str(model_id)] = parsed
        else:
            continue
        records[str(provider_id)] = {"models": models, "metadata": metadata}
    return records


def _load_model_cache_records() -> dict[str, dict[str, Any]]:
    if not _cache_path().exists():
        return {}
    try:
        with _cache_path().open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    return _coerce_model_cache_records(data)


def _write_model_cache_records(records: dict[str, dict[str, Any]]) -> None:
    _ensure_config_dir()
    payload: dict[str, Any] = {}
    for provider_id, record in records.items():
        models = [str(model_id) for model_id in record.get("models", []) if str(model_id)]
        model_set = set(models)
        metadata_raw = record.get("metadata")
        metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
        filtered_metadata = {
            str(model_id): _metadata_payload(item)
            for model_id, item in metadata.items()
            if str(model_id) in model_set and isinstance(item, ModelCacheMetadata)
        }
        if filtered_metadata:
            payload[provider_id] = {"models": models, "metadata": filtered_metadata}
        else:
            payload[provider_id] = models
    with _cache_path().open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def load_model_cache() -> dict[str, list[str]]:
    records = _load_model_cache_records()
    return {provider_id: list(record["models"]) for provider_id, record in records.items()}


def load_model_cache_metadata() -> dict[str, dict[str, ModelCacheMetadata]]:
    records = _load_model_cache_records()
    out: dict[str, dict[str, ModelCacheMetadata]] = {}
    for provider_id, record in records.items():
        metadata = record.get("metadata")
        if isinstance(metadata, dict):
            out[provider_id] = {
                str(model_id): item
                for model_id, item in metadata.items()
                if isinstance(item, ModelCacheMetadata)
            }
    return out


def save_model_cache(cache: dict[str, list[str]]) -> None:
    current = _load_model_cache_records()
    records: dict[str, dict[str, Any]] = {}
    for provider_id, models in cache.items():
        metadata = current.get(provider_id, {}).get("metadata", {})
        records[provider_id] = {"models": [str(m) for m in models], "metadata": metadata}
    _write_model_cache_records(records)


def cache_models(
    provider_id: str,
    models: list[str],
    metadata: dict[str, ModelCacheMetadata] | None = None,
) -> None:
    records = _load_model_cache_records()
    current_metadata = records.get(provider_id, {}).get("metadata", {})
    merged_metadata = dict(current_metadata) if isinstance(current_metadata, dict) else {}
    if metadata:
        merged_metadata.update(metadata)
    records[provider_id] = {"models": [str(m) for m in models], "metadata": merged_metadata}
    _write_model_cache_records(records)


def clear_model_cache(provider_id: str) -> None:
    cache = load_model_cache()
    if provider_id in cache:
        del cache[provider_id]
        save_model_cache(cache)


def _models_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/models"


def _is_openai_models_metadata_candidate(provider_def: providers.ProviderDef | None, login: Login) -> bool:
    if provider_def is None:
        return False
    base_url = login.provider_base_url or provider_def.default_base_url
    if not base_url:
        return False
    return (
        provider_def.transport in _OPENAI_MODEL_METADATA_TRANSPORTS
        or provider_def.effective_sdk_provider_id == "openai"
    )


async def _fetch_openai_model_metadata(
    login: Login,
    provider_def: providers.ProviderDef | None,
) -> dict[str, ModelCacheMetadata]:
    if not _is_openai_models_metadata_candidate(provider_def, login):
        return {}
    assert provider_def is not None
    base_url = login.provider_base_url or provider_def.default_base_url
    if not base_url:
        return {}
    headers = dict(login.provider_headers or {})
    if login.provider_api_key and not any(key.lower() == "authorization" for key in headers):
        headers["Authorization"] = f"Bearer {login.provider_api_key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(_models_url(base_url), headers=headers or None)
            response.raise_for_status()
            payload = response.json()
    except Exception:  # noqa: BLE001 - metadata is best-effort; model listing already succeeded
        return {}
    return metadata_from_openai_models_payload(payload)


async def _fetch_model_ids(login: Login) -> list[str]:
    if _normalize_provider_id(login.provider_id) == "openai-codex":
        from . import codex_provider

        return await codex_provider.fetch_models_for_login(login)

    provider_def = providers.get_provider(login.provider_id)
    shape_provider_def = providers.get_provider(login.shape_provider_id) if login.shape_provider_id else None
    endpoint_provider_def = provider_def or shape_provider_def
    if endpoint_provider_def is not None:
        providers.assert_endpoint_configured(endpoint_provider_def, login.provider_base_url)
    provider = ai.get_provider(
        login.effective_provider_id,
        base_url=login.provider_base_url,
        api_key=login.provider_api_key,
        headers=login.provider_headers or None,
    )
    try:
        model_ids = await provider.list_models()
    finally:
        aclose = getattr(provider, "aclose", None)
        if callable(aclose):
            await aclose()
    # No client-side allowlist: the live API list is the source of truth, so the
    # login picker shows exactly what the endpoint serves (e.g. opencode-go's
    # glm-5.2) instead of a curated tuple that goes stale the moment a model ships.
    return [str(mid) for mid in model_ids]


async def fetch_models(login: Login) -> list[str]:
    """Fetch model ids from a provider; raises on failure."""
    return await _fetch_model_ids(login)


async def fetch_models_with_metadata(login: Login) -> tuple[list[str], dict[str, ModelCacheMetadata]]:
    """Fetch model ids plus best-effort server-advertised per-model limits."""
    models = await _fetch_model_ids(login)
    provider_def = providers.get_provider(login.provider_id)
    shape_provider_def = providers.get_provider(login.shape_provider_id) if login.shape_provider_id else None
    metadata_provider_def = provider_def or shape_provider_def
    metadata = await _fetch_openai_model_metadata(login, metadata_provider_def)
    return models, metadata


def test_login(login: Login) -> list[str]:
    """Validate a login by fetching its model list. Returns models or raises."""
    return asyncio.run(fetch_models(login))


def test_login_with_metadata(login: Login) -> tuple[list[str], dict[str, ModelCacheMetadata]]:
    """Validate a login and return fetched models plus best-effort metadata."""
    return asyncio.run(fetch_models_with_metadata(login))


def login_path() -> Path:
    _ensure_config_dir()
    return _logins_path()
