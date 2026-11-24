"""A model's context window: the operator's overrides first, then what a local server reports, then models.dev."""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from . import (
    compaction_settings,
    context_budget,
    model_client,
    model_metadata,
    output,
    providers,
    settings as _settings,
    toolkit as T,
)
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


def thresholds(cfg: Config) -> tuple[float, float, float]:
    notify_at = compaction_settings.get_float(cfg, "COMPACTION_WARN_AT", max_value=1.0)
    trigger_at = compaction_settings.get_float(cfg, "COMPACTION_START_AT", max_value=1.0)
    force_at = compaction_settings.get_float(cfg, "COMPACTION_FORCE_AT", max_value=1.0)
    if not (notify_at <= trigger_at <= force_at):
        return (
            _settings.default_value("COMPACTION_WARN_AT"),
            _settings.default_value("COMPACTION_START_AT"),
            _settings.default_value("COMPACTION_FORCE_AT"),
        )
    return notify_at, trigger_at, force_at


def configured_context_window(cfg: Config, resolve_window: Any) -> int:
    """One configured window for the banner and both compaction triggers:
    CONTEXT_SIZE, else the resolved window, else CONTEXT_SIZE_UNKNOWN."""
    window = compaction_settings.get_int(cfg, "CONTEXT_SIZE", 0) or int(resolve_window() or 0)
    return window if window > 0 else compaction_settings.get_int(cfg, "CONTEXT_SIZE_UNKNOWN")


def effective_context_window(cfg: Config, context_window: int) -> int:
    """Window minus the room the next turn already owes: one full reply plus the
    compaction buffer. Both triggers measure against this so they agree on what
    'full' means. A fraction of the raw window does not survive a small model —
    0.8 of 32k leaves 6.4k, less than a single max-length reply, so compaction
    would arm only after the turn it needed to prevent."""
    if context_window <= 0:
        return 0
    max_out = cfg.max_output_tokens
    if max_out is None:
        max_out = model_metadata.resolve_max_output(cfg.model, cfg.provider_id)
    # Reserve what a reply plausibly costs, NOT what the model is willing to
    # emit. gpt-5.6-sol declares max_output 128000; holding all of that back
    # would hand a third of a 370k window to an outcome that essentially never
    # happens. Cap it the way Claude Code does (min(max_output, 20k)).
    reserve = min(
        max(0, int(max_out or 0)),
        compaction_settings.get_nonnegative_int(cfg, "COMPACTION_REPLY_RESERVE_TOKENS"),
    )
    buffer_tokens = compaction_settings.get_nonnegative_int(cfg, "COMPACTION_HEADROOM_TOKENS")
    # Never let the reserve eat the whole window on a model with a huge declared
    # output cap; keep at least half the window addressable for input.
    return max(context_window // 2, context_window - reserve - buffer_tokens)


def attachment_room_bytes(cfg: Config, system: str = "", messages: list[dict] | None = None) -> int:
    """Bytes of file text one message's attachments may take together:
    ATTACHMENT_CONTEXT_MAX of the room left before compaction starts, after
    ``system`` and ``messages``, at COMPACTION_CHARS_PER_TOKEN. The rest of
    that room is the turn's own, for its tool calls and their results."""
    window = configured_context_window(
        cfg, lambda: resolve_context_window(cfg.model, cfg.provider_id, getattr(cfg, "provider_base_url", None)))
    _, start_at, _ = thresholds(cfg)
    used = estimated_prompt_tokens(cfg, system, messages) if system or messages else 0
    room_tokens = int(effective_context_window(cfg, window) * start_at) - used
    share = compaction_settings.get_float(cfg, "ATTACHMENT_CONTEXT_MAX", max_value=1.0)
    chars_per_token = compaction_settings.get_float(cfg, "COMPACTION_CHARS_PER_TOKEN")
    return max(0, int(room_tokens * share * chars_per_token))


def message_text_for_estimate(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)


def history_chars(messages: list[dict]) -> int:
    return sum(len(message_text_for_estimate(m)) for m in messages)


def estimate_tokens(messages: list[dict], chars_per_token: float = 4.0) -> int:
    ratio = chars_per_token if chars_per_token and chars_per_token > 0 else 4.0
    return int(history_chars(messages) / ratio)


def estimated_prompt_tokens(cfg: Config, system: str, messages: list[dict]) -> int:
    """Local estimate of what the next request will cost, in tokens.

    Only used when the provider reported nothing — which is exactly the case
    after a turn that failed for being too large. Without this the trigger
    reads 0, does nothing, and the history that caused the failure survives
    into the next attempt unchanged.
    """
    cpt = compaction_settings.get_float(cfg, "COMPACTION_CHARS_PER_TOKEN")
    try:
        return (
            context_budget.estimate_messages_tokens(messages or [], chars_per_token=cpt)
            + context_budget.estimate_system_tokens(system or "", chars_per_token=cpt)
        )
    except Exception as exc:  # noqa: BLE001 - an estimate must never break the turn
        output.failed_once("compaction token estimate", exc)
        return 0


def calibrated_chars_per_token(cfg: Config, system: str, messages: list[dict], context: Any = None) -> float:
    """Configured chars_per_token, corrected against real provider counts.

    tail_tokens and min_savings_tokens used the raw 4.0 estimate while the
    trigger that called them used provider tokens. On a tool-heavy session the
    estimator drifts far enough that "keep 16k of tail" kept something quite
    different from 16k.
    """
    configured = compaction_settings.get_float(cfg, "COMPACTION_CHARS_PER_TOKEN")
    try:
        # The tracker lives on the tool context (set in run_turn_async), not in
        # module scope — reading a global here would silently always miss.
        context = context or T.STOCK_CONTEXT
        tracker = context.context_budget_state
        if tracker is None:
            return configured
        registry = context.tool_registry
        tools = model_client.tool_specs_to_ai_tools(registry.openai_specs()) if registry else None
        return tracker.calibrated_chars_per_token(messages=messages, system=system, tools=tools)
    except Exception as exc:  # noqa: BLE001 - calibration must never break compaction
        output.failed_once("compaction chars-per-token calibration", exc)
        return configured
