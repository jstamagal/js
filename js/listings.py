"""The `--list-*` printers: sessions, providers, logins and models, as text or JSON."""

from __future__ import annotations

import json
import time
from dataclasses import asdict

from . import (
    logins,
    messages as msgs,
    paranoid,
    paths as _paths,
    providers,
    session_query,
)
from .config import (
    Config,
)
from .session_catalog import (
    catalog_sessions,
)


def print_session_list(*, as_json: bool) -> int:
    """Every session, newest first; the table shows times in local time as the
    picker does."""
    records = sorted(catalog_sessions(_paths.sessions_root()), key=lambda record: -record["mtime"])
    if as_json:
        print(json.dumps(records, separators=(",", ":"), ensure_ascii=True))
        return 0

    headings = tuple(msgs.LIST_HEADINGS.words().split())
    now = time.time()
    rows = []
    for record in records:
        identity = record["caller_key"]
        if record["job_id"] is not None:
            identity = str(record["job_id"]) if identity is None else f"{identity}/{record['job_id']}"
        rows.append(
            (
                record["agent"] or "-",
                record["name"],
                session_query.when_text(record["mtime"], now),
                str(record["size"]),
                str(record["user_turns"]),
                (msgs.LIST_YES if record["in_flight"] else msgs.LIST_NO).words(),
                record["cwd"] or "-",
                identity or "-",
            )
        )
    widths = [len(value) for value in headings]
    for row in rows:
        widths = [max(width, len(value)) for width, value in zip(widths, row, strict=True)]
    print("  ".join(value.ljust(width) for value, width in zip(headings, widths, strict=True)))
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)))
    return 0


def providers_json() -> list[dict]:
    saved = logins.load_logins()
    known_ids = {p.id for p in providers.login_providers()}
    rows: list[dict] = []
    for provider in providers.login_providers():
        env_configured = providers.first_env(provider.api_key_env + provider.base_url_env + provider.model_env) is not None
        source = "login" if provider.id in saved else ("env" if env_configured else "registry")
        rows.append({"id": provider.id, "name": provider.display_name, "source": source})
    for provider_id in sorted(set(saved) - known_ids):
        rows.append({"id": provider_id, "name": provider_id, "source": "custom"})
    return rows


def logins_json() -> list[dict]:
    rows: list[dict] = []
    masked = paranoid.masks(paranoid.mode())
    for provider_id, login in sorted(logins.load_logins().items()):
        item = asdict(login)
        item["provider_id"] = provider_id
        item["has_api_key"] = bool(item.get("provider_api_key"))
        if masked:
            item["provider_api_key"] = None
            item["provider_headers"] = {}
        rows.append(item)
    return rows


def _models_cached_or_live(provider_id: str, cfg: Config | None) -> list[str]:
    """Models for one provider, cache first (offline-friendly, fast), live on miss."""
    from . import cli as _cli  # cli imports this module

    cached = logins.load_model_cache().get(provider_id)
    if cached is not None:
        return cached
    if cfg is not None and provider_id == cfg.provider_id:
        return _cli._models_for_provider(provider_id, cfg.provider_base_url, cfg.provider_api_key)
    return _cli._models_for_provider(provider_id, None, None)


def _list_models_provider_ids(provider_arg: str | None, cfg: Config | None) -> list[str]:
    """Which providers `--list-models` covers: the named one, else every provider
    the operator has a stake in — saved logins plus the active provider."""
    if provider_arg:
        return [providers.normalize_provider_id(provider_arg) or provider_arg]
    ids = set(logins.load_logins())
    if cfg is not None and cfg.provider_id:
        ids.add(cfg.provider_id)
    return sorted(ids)


def print_model_list(provider_arg: str | None, cfg: Config | None) -> int:
    """Print one `provider/model` line per model of `provider_arg`, or of
    every saved login when it is None. A provider whose models cannot be
    listed gets a `#` line on stderr and the rest still print."""
    provider_ids = _list_models_provider_ids(provider_arg, cfg)
    if not provider_ids:
        msgs.warn(msgs.NO_LOGINS)
        return 0
    for pid in provider_ids:
        try:
            model_ids = _models_cached_or_live(pid, cfg)
        except Exception as e:  # noqa: BLE001
            msgs.warn(msgs.MODEL_LIST_FAILED, provider=pid, error=f"{type(e).__name__}: {e}")
            continue
        for model_id in model_ids:
            print(f"{pid}/{model_id}" if pid else model_id)
    return 0


def models_json(provider_id: str | None, cfg: Config | None = None) -> dict:
    models = []
    for provider in _list_models_provider_ids(provider_id, cfg):
        models.extend(f"{provider}/{model}" if provider else model
                      for model in _models_cached_or_live(provider, cfg))
    return {"models": models}


def print_json(value: object) -> int:
    print(json.dumps(value, ensure_ascii=False))
    return 0

