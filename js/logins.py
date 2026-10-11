"""Multi-provider login state and cached model lists.

Logins and model caches live in ~/.js/logins.  Provider ids
are user-facing js ids; the registry decides the SDK/API shape used at runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import ai
import httpx
import tomli_w
import tomllib

from . import messages as msgs
from . import paths, providers

_CONFIG_DIR_OVERRIDE: Path | None = None
_LOGINS_FILE = "logins.toml"
_CACHE_FILE = "models-cache.json"
_LOCK_FILE = "logins.toml.lock"
# How long a caller waits for the store lock. Longer than the refresh POST
# bound (20s in codex_auth/xai_auth) so a waiter outlives the holder's POST.
_LOCK_TIMEOUT_S = 30.0
_LOCK_POLL_S = 0.05


class LoginStoreBusyError(RuntimeError):
    """The login store lock was not acquired within the timeout."""


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


def lock_path() -> Path:
    """The sidecar the store lock lives on.

    flock belongs to an open file description, and ``logins.toml`` itself is
    replaced by ``os.replace`` on every save, so a lock on the data file would
    be lost with the old inode. The sidecar is only ever opened and locked,
    never replaced or unlinked.
    """
    return _config_dir() / _LOCK_FILE


def _open_lock_fd() -> int:
    _ensure_config_dir()
    return os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)


def _try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextlib.contextmanager
def hold_login_lock(timeout_s: float = _LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold the cross-process store lock for the block (sync callers).

    Non-blocking flock in a sleep loop, never a blocking flock: a blocking
    flock has no timeout, and the sync callers (login_cli, routing) must
    not hang forever behind a stuck holder. Raises LoginStoreBusyError on
    timeout. Re-entrant per thread, so save_login inside a held lock works.
    """
    if _lock_depth.held:
        _lock_depth.held += 1
        try:
            yield
        finally:
            _lock_depth.held -= 1
        return
    fd = _open_lock_fd()
    try:
        deadline = time.monotonic() + timeout_s
        while not _try_flock(fd):
            if time.monotonic() >= deadline:
                raise LoginStoreBusyError(f"{lock_path()} held by another process for {timeout_s:g}s")
            time.sleep(_LOCK_POLL_S)
        _lock_depth.held = 1
        try:
            yield
        finally:
            _lock_depth.held = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.asynccontextmanager
async def ahold_login_lock(timeout_s: float = _LOCK_TIMEOUT_S) -> AsyncIterator[None]:
    """Async twin of hold_login_lock: the same non-blocking flock, polled with
    ``asyncio.sleep`` so the event loop never blocks. A waiter cancelled
    mid-poll holds nothing and just closes its fd; a holder's ``finally``
    unlocks on every exit, including cancellation.

    Never ``to_thread`` a blocking flock here: a cancelled waiter would leave
    a thread that later acquires and holds the lock with nobody to release
    it.
    """
    if _lock_depth.held:
        _lock_depth.held += 1
        try:
            yield
        finally:
            _lock_depth.held -= 1
        return
    fd = _open_lock_fd()
    try:
        deadline = time.monotonic() + timeout_s
        while not _try_flock(fd):
            if time.monotonic() >= deadline:
                raise LoginStoreBusyError(f"{lock_path()} held by another process for {timeout_s:g}s")
            await asyncio.sleep(_LOCK_POLL_S)
        _lock_depth.held = 1
        try:
            yield
        finally:
            _lock_depth.held = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class _LockDepth(threading.local):
    held = 0


_lock_depth = _LockDepth()

# One asyncio.Lock per provider per event loop: coalesces concurrent
# refreshes inside one process before any of them touches the file lock.
# Always taken in-process first, then the file lock, never the reverse.
_refresh_locks: dict[tuple[int, str], asyncio.Lock] = {}
_refresh_locks_guard = threading.Lock()


def refresh_lock(provider_id: str) -> asyncio.Lock:
    """The in-process refresh lock for ``provider_id`` on the running loop."""
    loop = asyncio.get_running_loop()
    key = (id(loop), _normalize_provider_id(provider_id))
    with _refresh_locks_guard:
        lock = _refresh_locks.get(key)
        if lock is None:
            lock = _refresh_locks[key] = asyncio.Lock()
        return lock


# Logins whose refreshed token could not be written to disk, kept for the
# life of this process so the next read uses the rotated token instead of
# re-posting the burned one (bakari #207). get_login() merges it in.
_overlay: dict[str, Login] = {}
_overlay_guard = threading.Lock()


def note_memory_login(login: Login) -> None:
    """Remember a refreshed login that did not make it to disk."""
    with _overlay_guard:
        _overlay[_normalize_provider_id(login.provider_id)] = login


def forget_memory_login(provider_id: str) -> None:
    with _overlay_guard:
        _overlay.pop(_normalize_provider_id(provider_id), None)


def _expiry(login: Login) -> float:
    expires = login.codex_token_expires if login.codex_refresh_token else login.xai_token_expires
    return float(expires) if expires is not None else 0.0


def get_login(provider_id: str) -> Login | None:
    """The stored login for ``provider_id``, or the fresher in-memory one.

    The sole read for the OAuth refresh paths: the overlay wins only while it
    is newer than what the file says, so a later successful save (or a
    ``--login`` / ``--logout`` from any process) takes over at once.
    """
    normalized = _normalize_provider_id(provider_id)
    stored = load_logins().get(normalized)
    with _overlay_guard:
        memory = _overlay.get(normalized)
    if memory is None:
        return stored
    if stored is None:
        # Removed on disk since: honour the removal.
        forget_memory_login(normalized)
        return None
    if _expiry(stored) >= _expiry(memory):
        forget_memory_login(normalized)
        return stored
    return memory


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
    # xAI OAuth metadata, same arrangement as the Codex fields above. The token
    # endpoint is stored rather than pinned because it comes from xAI's own
    # discovery document.
    xai_refresh_token: str | None = None
    xai_token_expires: float | None = None
    xai_token_endpoint: str | None = None
    xai_email: str | None = None

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


_STR_FIELDS = (
    "sdk_provider_id",
    "shape_provider_id",
    "provider_base_url",
    "provider_api_key",
    "codex_refresh_token",
    "codex_account_id",
    "codex_email",
    "xai_refresh_token",
    "xai_token_endpoint",
    "xai_email",
)
_EXPIRY_FIELDS = ("codex_token_expires", "xai_token_expires")
_KNOWN_FIELDS = frozenset(f.name for f in fields(Login))


class _BadSection(ValueError):
    """A logins.toml section whose values have the wrong shape."""


def _optional_str(raw: dict[str, Any], name: str) -> str | None:
    value = raw.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _BadSection(f"{name} must be a string, not {type(value).__name__}")
    return value


def _optional_expiry(raw: dict[str, Any], name: str) -> float | None:
    """A token expiry as a float, or None when unreadable.

    None reads as "expired" to login_needs_refresh, so a hand-edited
    ``codex_token_expires = "soon"`` costs one refresh instead of crashing
    every request.
    """
    value = raw.get(name)
    if value is None or isinstance(value, bool):
        return None
    parsed: float | None = None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            parsed = None
    # nan compares False against everything, so a nan expiry would read as
    # "fresh forever"; inf would never refresh either. Both mean "unknown".
    return parsed if parsed is not None and math.isfinite(parsed) else None


def _login_from_section(provider_id: str, raw: dict[str, Any]) -> Login:
    """Build a Login from one TOML table; raises _BadSection on wrong types."""
    stored_id = raw.get("provider_id", provider_id)
    if not isinstance(stored_id, str) or not stored_id.strip():
        raise _BadSection(f"provider_id must be a non-empty string, not {stored_id!r}")
    headers_raw = raw.get("provider_headers")
    if headers_raw is None:
        headers_raw = {}
    if not isinstance(headers_raw, dict):
        raise _BadSection("provider_headers must be a table")
    strings = {name: _optional_str(raw, name) for name in _STR_FIELDS}
    return Login(
        provider_id=providers.normalize_provider_id(stored_id) or stored_id,
        provider_headers={str(k): str(v) for k, v in headers_raw.items()},
        codex_token_expires=_optional_expiry(raw, "codex_token_expires"),
        xai_token_expires=_optional_expiry(raw, "xai_token_expires"),
        **strings,
    )


@dataclass(frozen=True)
class _Store:
    """One parse of logins.toml: the usable logins plus everything a rewrite
    must carry through verbatim. Travels as one value so a quarantined table
    can never outlive the file it came from (a config-dir switch, a deleted
    file, a fresh store)."""

    ident: tuple | None
    logins: dict[str, Login]
    quarantined: dict[str, Any]
    extra_fields: dict[str, dict[str, Any]]


_EMPTY_STORE = _Store(None, {}, {}, {})


def _parse_store(ident: tuple | None, data: dict[str, Any]) -> _Store:
    """Split a parsed logins.toml into usable logins and quarantined tables.

    A section whose values have the wrong shape (a hand edit, ``provider_id
    = 5``) is kept verbatim in ``quarantined``, so a later save writes it
    back unchanged instead of deleting it. Unknown keys inside a good
    section are kept the same way (``extra_fields``), so a newer js writing
    a field this checkout does not know survives a save from here.
    """
    logins: dict[str, Login] = {}
    quarantined: dict[str, Any] = {}
    extra_fields: dict[str, dict[str, Any]] = {}
    for provider_id, raw in data.items():
        if not isinstance(raw, dict):
            quarantined[provider_id] = raw
            msgs.warn(msgs.ENV_SETTING_IGNORED, name=f"logins.toml [{provider_id}]", error="not a table")
            continue
        try:
            login = _login_from_section(provider_id, raw)
        except _BadSection as exc:
            quarantined[provider_id] = raw
            msgs.warn(msgs.ENV_SETTING_IGNORED, name=f"logins.toml [{provider_id}]", error=exc)
            continue
        key = providers.normalize_provider_id(provider_id) or provider_id
        logins[key] = login
        extra = {k: v for k, v in raw.items() if k not in _KNOWN_FIELDS}
        if extra:
            extra_fields[key] = extra
    return _Store(ident, logins, quarantined, extra_fields)


def _file_ident(path: Path) -> tuple | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_store() -> _Store:
    """Parse logins.toml now, bypassing the identity cache, and cache the
    result. An empty store when the file is missing or unparseable."""
    global _loaded
    path = _logins_path()
    ident = _file_ident(path)
    if ident is None:
        _loaded = _EMPTY_STORE
        return _loaded
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except Exception:  # noqa: BLE001
        _loaded = _EMPTY_STORE
        return _loaded
    _loaded = _parse_store(ident, data)
    return _loaded


def _current_store() -> _Store:
    """The cached parse while the file is unchanged, else a fresh one."""
    ident = _file_ident(_logins_path())
    if ident is None:
        return _read_store()
    if _loaded.ident == ident:
        return _loaded
    return _read_store()


def load_logins_fresh() -> dict[str, Login]:
    """load_logins() that always re-reads the file.

    For a holder of the store lock deciding whether to refresh: the cached
    parse may predate another process's save, and only the bytes on disk
    say whether the token was already rotated.
    """
    return dict(_read_store().logins)


def load_logins() -> dict[str, Login]:
    """Return map provider_id -> Login.

    A malformed file degrades to {} here — this runs on every routing/picker
    call and must never crash a turn. The write path (save_login/
    remove_login) calls _refuse_if_corrupt() first instead, since silently
    treating a corrupt file as empty there would truncate every stored login.
    A single section with wrong-typed values (valid TOML, bad shape) is
    skipped with one warning and quarantined: the other logins load, and a
    later save writes the bad section back untouched.

    The file is parsed once per version of it: the result is kept with the
    file's identity (path, device, inode, size, mtime) and returned again,
    as a copy, while the file is unchanged. A write from this process or
    another one changes that identity and the next call parses again.
    """
    return dict(_current_store().logins)


_loaded: _Store = _EMPTY_STORE


def _serialize(store: _Store, loaded: dict[str, Login]) -> dict[str, dict[str, Any]]:
    """The TOML tables for ``loaded``, plus what ``store`` says must be
    carried through unchanged: unknown keys of good sections and whole
    quarantined sections."""
    data: dict[str, dict[str, Any]] = {}
    for lid, item in loaded.items():
        row = {k: v for k, v in asdict(item).items() if v not in (None, {}, [])}
        for k, v in store.extra_fields.get(lid, {}).items():
            row.setdefault(k, v)
        data[lid] = row
    for lid, raw in store.quarantined.items():
        data.setdefault(lid, raw)
    return data


def save_login(login: Login) -> None:
    """Add or update a provider login and persist.

    Load-modify-write under the store lock, so two processes saving at once
    (a Codex refresh and an xAI refresh, say) cannot drop each other's
    change. Raises LoginsCorruptError instead of proceeding if the existing
    file fails to parse (see _refuse_if_corrupt).
    """
    _ensure_config_dir()
    canonical_id = providers.normalize_provider_id(login.provider_id) or login.provider_id
    login = replace(login, provider_id=canonical_id)
    with hold_login_lock():
        _refuse_if_corrupt()
        store = _read_store()
        loaded = dict(store.logins)
        loaded[canonical_id] = login
        _write_logins_toml(_serialize(store, loaded))
    forget_memory_login(canonical_id)


def _normalize_provider_id(provider_id: str) -> str:
    return providers.normalize_provider_id(provider_id) or provider_id


def remove_login(provider_id: str) -> bool:
    """Remove a provider login. Returns True if it existed.

    Raises LoginsCorruptError instead of proceeding if the existing file
    fails to parse (see _refuse_if_corrupt).
    """
    provider_id = _normalize_provider_id(provider_id)
    with hold_login_lock():
        _refuse_if_corrupt()
        store = _read_store()
        loaded = dict(store.logins)
        existed = provider_id in loaded
        if existed:
            del loaded[provider_id]
            _ensure_config_dir()
            _write_logins_toml(_serialize(store, loaded))
        # Also drop its cached models.
        cache = load_model_cache()
        if provider_id in cache:
            del cache[provider_id]
            save_model_cache(cache)
    forget_memory_login(provider_id)
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


class _CacheUnreadable(RuntimeError):
    """models-cache.json exists but could not be parsed."""


def _read_model_cache_records() -> dict[str, dict[str, Any]]:
    """Parse the cache; raises _CacheUnreadable instead of pretending it is
    empty, so a writer never replaces a file it could not read."""
    if not _cache_path().exists():
        return {}
    try:
        with _cache_path().open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise _CacheUnreadable(f"{_cache_path()}: {exc}") from exc
    return _coerce_model_cache_records(data)


def _load_model_cache_records() -> dict[str, dict[str, Any]]:
    try:
        return _read_model_cache_records()
    except _CacheUnreadable:
        return {}


def _write_model_cache_records(records: dict[str, dict[str, Any]]) -> None:
    """Write the cache atomically (temp file + rename), like logins.toml.

    The old in-place ``open("w")`` truncated first and wrote second; a
    reader between the two saw an empty or half file, parsed it as {} and
    the next writer persisted only its own provider: every other cached
    model list gone (ALL-CLAIMS logins.py:428).
    """
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
    path = _cache_path()
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


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
    """Replace the cached model lists with ``cache`` (metadata kept per provider).

    Load-merge-write under the store lock: two processes caching different
    providers at once must not drop each other's list. A cache file that
    cannot be parsed is not overwritten.
    """
    with hold_login_lock():
        current = _read_model_cache_records()
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
    with hold_login_lock():
        records = _read_model_cache_records()
        current_metadata = records.get(provider_id, {}).get("metadata", {})
        merged_metadata = dict(current_metadata) if isinstance(current_metadata, dict) else {}
        if metadata:
            merged_metadata.update(metadata)
        records[provider_id] = {"models": [str(m) for m in models], "metadata": merged_metadata}
        _write_model_cache_records(records)


def clear_model_cache(provider_id: str) -> None:
    with hold_login_lock():
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
