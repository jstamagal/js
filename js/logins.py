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


def load_model_cache() -> dict[str, list[str]]:
    if not _cache_path().exists():
        return {}
    try:
        with _cache_path().open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): [str(m) for m in v] for k, v in data.items() if isinstance(v, list)}


def save_model_cache(cache: dict[str, list[str]]) -> None:
    _ensure_config_dir()
    with _cache_path().open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def cache_models(provider_id: str, models: list[str]) -> None:
    cache = load_model_cache()
    cache[provider_id] = models
    save_model_cache(cache)


def clear_model_cache(provider_id: str) -> None:
    cache = load_model_cache()
    if provider_id in cache:
        del cache[provider_id]
        save_model_cache(cache)


async def fetch_models(login: Login) -> list[str]:
    """Fetch model ids from a provider; raises on failure."""
    if _normalize_provider_id(login.provider_id) == "openai-codex":
        from . import codex_provider

        return await codex_provider.fetch_models_for_login(login)

    provider_def = providers.get_provider(login.provider_id)
    if provider_def is not None:
        providers.assert_endpoint_configured(provider_def, login.provider_base_url)
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


def test_login(login: Login) -> list[str]:
    """Validate a login by fetching its model list. Returns models or raises."""
    return asyncio.run(fetch_models(login))


def login_path() -> Path:
    _ensure_config_dir()
    return _logins_path()
