"""A model's context window: the operator's overrides first, then what a local server reports, then models.dev."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from . import compaction_settings, model_metadata, providers
from .config import Config

_CONTEXT_WINDOW_OVERRIDES: ContextVar[dict[str, int] | None] = ContextVar(
    "context_window_overrides", default=None,
)


def set_context_window_overrides(raw: Any) -> None:
    """Install CONTEXT_SIZE_PER_MODEL, a map of model key -> window.

    models.dev carries one row per model id, not per *surface*. A subscription
    or proxy endpoint and the public API can serve the same model id with
    different usable windows, and the catalog has no row for the former — so
    there is no upstream truth to fetch, only a local fact the operator knows.
    Keys are matched most specific first: "provider/model", then "model".
    """
    overrides: dict[str, int] = {}
    if not isinstance(raw, dict):
        _CONTEXT_WINDOW_OVERRIDES.set(overrides)
        return

    def walk(node: Any, prefix: str) -> None:
        # `set a.b.c 1` splits the key on every dot, so a model id like
        # gpt-5.6-sol arrives as nested dicts ({"gpt-5": {"6-sol": ...}}).
        # Rejoining with "." puts the id back together.
        for key, value in node.items():
            if not isinstance(key, str) or not key.strip():
                continue
            path = f"{prefix}.{key.strip()}" if prefix else key.strip()
            if isinstance(value, dict):
                walk(value, path)
                continue
            try:
                window = int(value)
            except (TypeError, ValueError):
                continue
            if window > 0:
                overrides[path.lower()] = window

    walk(raw, "")
    _CONTEXT_WINDOW_OVERRIDES.set(overrides)


def install_context_window_overrides(cfg: Config) -> None:
    """Install both override forms for this turn.

    CONTEXT_SIZE is the single-model form, symmetric with
    OUTPUT_TOKENS_MAX, and wins — it names the model actually running.
    CONTEXT_SIZE_PER_MODEL is the multi-model map.
    """
    overrides = dict(compaction_settings.get(cfg, "CONTEXT_SIZE_PER_MODEL", None) or {})
    explicit = getattr(cfg, "model_context_window", None)
    if explicit:
        key = f"{cfg.provider_id}/{cfg.model}" if cfg.provider_id else cfg.model
        overrides[key] = explicit
    set_context_window_overrides(overrides)


def _context_window_override(model: str, provider_id: str | None) -> int | None:
    overrides = _CONTEXT_WINDOW_OVERRIDES.get()
    if not overrides:
        return None
    name = (model or "").strip().lower()
    keys = []
    if provider_id:
        keys.append(f"{provider_id.strip().lower()}/{name}")
    keys.append(name)
    for key in keys:
        hit = overrides.get(key)
        if hit is not None:
            return hit
    return None


def resolve_context_window(
    model: str,
    provider_id: str | None,
    provider_base_url: str | None = None,
) -> int | None:
    """Use explicit overrides, local runtime allocation, then models.dev."""
    override = _context_window_override(model, provider_id)
    if override is not None:
        return override
    provider = providers.get_provider(provider_id)
    # An explicit non-default endpoint owns the truth about its own context
    # window, whatever transport the provider definition uses.
    custom_endpoint = provider is not None and providers.is_custom_base_url(provider, provider_base_url)
    local_runtime = provider is not None and (
        provider.transport in {"ollama", "llama.cpp"}
        or provider.id == "vllm"
        or custom_endpoint
    )
    if local_runtime:
        probed = model_metadata.probe_local_context_window(
            model, provider_id, base_url=provider_base_url,
        )
        if probed is not None:
            return probed
        cached = model_metadata.cached_server_limits(model, provider_id)
        if cached is not None and cached.context_window is not None:
            return cached.context_window
    catalog = model_metadata.context_window(model, provider_id)
    ceiling = model_metadata.cached_server_context_ceiling(model, provider_id) if local_runtime else None
    return min(catalog, ceiling) if catalog is not None and ceiling is not None else catalog
