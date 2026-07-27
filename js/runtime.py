"""Tool-use loop. Streaming output, typed error handling, telemetry.
Uses ``js.model_client`` for model I/O via the Vercel AI Python SDK (``ai``)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
import contextlib
import inspect
import json
import hashlib
import os
import sys
import threading
from pathlib import Path
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any

from . import events as event_mod
from . import model_client
import ai

from . import colors as C
from . import context_budget
from . import model_metadata
from . import tools as T
from . import tool_args
from . import routing
from .capped_process import CappedProcessResult, _run_capped, truncation_marker
from .config import Config, vision_enabled_for_model
from .sampling import Sampling
from .toolkit.core import ToolContext, call_tool
from .toolkit.registry import ToolRegistry


_UNSET = object()


def _usable_tool_aliases(alias_map: dict[str, str], tool_names: set[str]) -> dict[str, str]:
    original_name_owners = {name.lower(): name for name in tool_names}
    reserved_names = {"tool_discovery"}
    return {
        canon: alias
        for canon, alias in alias_map.items()
        if canon in tool_names
        and alias.lower() not in reserved_names
        and (original_name_owners.get(alias.lower()) in (None, canon))
    }


def _resolve_alias_profile(
    settings: dict,
    model: str,
    provider_id: str | None,
    registry: ToolRegistry | None = None,
) -> dict[str, str]:
    """Pick the canonical->alias map for the first matching tool alias profile.

    Profiles live under ``[[tools.alias_profiles]]`` in config; each entry has
    a ``match`` string or list of case-insensitive substrings tested against the
    model id and provider id, plus an ``aliases`` table mapping canonical tool
    names to the model-facing names. The first profile whose ``match`` hits
    wins. No profiles configured (the default) → empty map → default tool
    names. When a registry is provided, matching profiles with no usable
    aliases are skipped.
    """
    tools_cfg = (settings or {}).get("tools")
    profiles = tools_cfg.get("alias_profiles") if isinstance(tools_cfg, dict) else None
    if not isinstance(profiles, list):
        return {}
    haystacks = [model.lower()]
    if provider_id:
        haystacks.append(str(provider_id).lower())
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        match = profile.get("match")
        if isinstance(match, str):
            match = [match]
        aliases = profile.get("aliases")
        if not isinstance(aliases, dict) or not isinstance(match, list):
            continue
        needles = [str(m).strip().lower() for m in match if str(m).strip()]
        if needles and any(n in h for n in needles for h in haystacks):
            alias_map = {str(k): str(v) for k, v in aliases.items() if str(v).strip()}
            if registry is not None:
                alias_map = _usable_tool_aliases(alias_map, set(registry.by_name))
                if not alias_map:
                    continue
            return alias_map
    return {}


def _aliased_tool_specs(specs: list[dict], alias_map: dict[str, str]) -> list[dict]:
    """Rewrite outgoing tool schema names and descriptions per ``alias_map``.

    Each spec's function name is swapped for its alias, and backtick-wrapped
    canonical names inside every tool description are rewritten so the
    cross-references the model reads stay consistent. ``specs`` is returned
    untouched (same object) when ``alias_map`` is empty."""
    if not alias_map:
        return specs
    original_names = {
        fn_name
        for spec in specs
        if isinstance((fn := spec.get("function")), dict)
        and isinstance((fn_name := fn.get("name")), str)
    }
    usable_aliases = _usable_tool_aliases(alias_map, original_names)
    if not usable_aliases:
        return specs
    desc_subs = {f"`{canon}`": f"`{alias}`" for canon, alias in usable_aliases.items()}
    transformed: list[dict] = []
    for spec in specs:
        cloned = json.loads(json.dumps(spec))
        fn = cloned.get("function", {})
        name = fn.get("name")
        if name in usable_aliases:
            fn["name"] = usable_aliases[name]
        desc = fn.get("description")
        if isinstance(desc, str) and desc:
            for needle, repl in desc_subs.items():
                if needle in desc:
                    desc = desc.replace(needle, repl)
            fn["description"] = desc
        transformed.append(cloned)
    return transformed


def _canonical_tool_call_name(name: str, registry: ToolRegistry) -> str:
    tool = registry.resolve(name)
    if tool is not None:
        return tool.name
    # An unloaded lazy tool has no Tool object in this dispatch batch, but
    # its alias still maps to a canonical name; persisted history must
    # record the canonical, never a configured alias.
    return registry.aliases.get(str(name).strip().lower(), name)


def _pending_with_name(pc: _PendingToolCall, name: str) -> _PendingToolCall:
    return _PendingToolCall(id=pc.id, name=name, arg_chunks=list(pc.arg_chunks))


def _resolve_max_output(model: str, provider_id: str | None) -> int | None:
    """Per-model output cap from server cache or models.dev metadata, else unset."""
    cached = model_metadata.cached_server_limits(model, provider_id)
    if cached is not None and cached.max_output_tokens is not None:
        return cached.max_output_tokens
    return model_metadata.max_output_tokens(model, provider_id)


def _provider_extra_params(cfg: Config) -> dict[str, Any] | None:
    provider_cfg = (getattr(cfg, "settings", {}) or {}).get("provider")
    extra = provider_cfg.get("extra") if isinstance(provider_cfg, dict) else None
    return dict(extra) if isinstance(extra, dict) else None


_CONTEXT_WINDOW_OVERRIDES: dict[str, int] = {}


def set_context_window_overrides(raw: Any) -> None:
    """Install compact.context_window_overrides, a map of model key -> window.

    models.dev carries one row per model id, not per *surface*. A subscription
    endpoint (openai-codex) and the public API can serve the same model id with
    different usable windows, and the catalog has no row for the former — so
    there is no upstream truth to fetch, only a local fact the operator knows.
    Keys are matched most specific first: "provider/model", then "model".
    """
    _CONTEXT_WINDOW_OVERRIDES.clear()
    if not isinstance(raw, dict):
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
                _CONTEXT_WINDOW_OVERRIDES[path.lower()] = window

    walk(raw, "")


def install_context_window_overrides(cfg: Config) -> None:
    """Install both override forms for this turn.

    model.context_window is the single-model form, symmetric with
    model.max_output_tokens, and wins — it names the model actually running.
    compact.context_window_overrides is the multi-model map.
    """
    overrides = dict(_compact_setting(cfg, "context_window_overrides", None) or {})
    explicit = getattr(cfg, "model_context_window", None)
    if explicit:
        key = f"{cfg.provider_id}/{cfg.model}" if cfg.provider_id else cfg.model
        overrides[key] = explicit
    set_context_window_overrides(overrides)


def _context_window_override(model: str, provider_id: str | None) -> int | None:
    if not _CONTEXT_WINDOW_OVERRIDES:
        return None
    name = (model or "").strip().lower()
    keys = []
    if provider_id:
        keys.append(f"{provider_id.strip().lower()}/{name}")
    keys.append(name)
    for key in keys:
        hit = _CONTEXT_WINDOW_OVERRIDES.get(key)
        if hit is not None:
            return hit
    return None


def _resolve_context_window(
    model: str,
    provider_id: str | None,
    provider_base_url: str | None = None,
) -> int | None:
    """Prefer local server-reported context windows, else models.dev metadata."""
    override = _context_window_override(model, provider_id)
    if override is not None:
        return override
    if (provider_id or "").strip().lower() == "openai-codex":
        from . import codex_models

        codex_window = codex_models.context_window(model)
        if codex_window is not None:
            return codex_window
    probed = model_metadata.probe_local_context_window(
        model,
        provider_id,
        base_url=provider_base_url,
    )
    if probed is not None:
        return probed
    cached = model_metadata.cached_server_limits(model, provider_id)
    if cached is not None and cached.context_window is not None:
        return cached.context_window
    catalog = model_metadata.context_window(model, provider_id)
    ceiling = model_metadata.cached_server_context_ceiling(model, provider_id)
    if catalog is not None and ceiling is not None:
        return min(catalog, ceiling)
    return catalog


# --------------------------------------------------------------------------
# Error taxonomy
# --------------------------------------------------------------------------

# Retry only ``ai.ProviderAPIError`` where ``exc.is_retryable`` is true.
# All other provider errors are fatal and abort the turn.


def _is_retriable(exc: BaseException) -> bool:
    if isinstance(exc, ai.ProviderAPIError):
        return bool(exc.is_retryable)
    return False


_CONTEXT_OVERFLOW_NEEDLES = (
    "context_length_exceeded",
    "context window",
    "context limit",
    "context length",
    "maximum context",
    "max context",
    "prompt too long",
    "prompt is too long",
    "input is too long",
    "input too long",
    "too many tokens",
    "token limit",
    "tokens exceed",
    "reduce the length",
    "request too large",
)


# How many times the summarizer may drop its oldest half and retry when the
# summarize call itself overflows.
_SUMMARY_PEEL_RETRIES = 3

# How many rounds of recovery a single turn may attempt after the provider
# rejects the request for size. One was never enough: the first compaction
# targets whatever window we *believe* we have, and if that belief is wrong
# the retry lands on the wall again.
_OVERFLOW_RECOVERY_ROUNDS = 3

MICROCOMPACT_CLEARED_MESSAGE = "[old tool result cleared]"

# After a summary compaction the model knows it edited a file but not what is
# in it, so its next move is to re-read everything it was just working on —
# one turn wasted, and the summary rarely carries enough detail to edit from.
# Re-attach the files it touched most recently, newest first, under a budget.
POST_COMPACT_MAX_FILES = 5
POST_COMPACT_TOKEN_BUDGET = 50_000
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000


def _post_compact_rehydration(
    context: Any,
    *,
    chars_per_token: float = 4.0,
    max_files: int = POST_COMPACT_MAX_FILES,
    token_budget: int = POST_COMPACT_TOKEN_BUDGET,
    per_file_tokens: int = POST_COMPACT_MAX_TOKENS_PER_FILE,
) -> dict | None:
    """Build one user message re-attaching recently read files, or None.

    Reads from disk rather than replaying the pre-compaction text, so the
    content is current — the model may have edited the file since. Files that
    vanished or grew past the per-file budget are named but not inlined, which
    is still useful: it tells the model the file exists and must be re-read.
    """
    paths = list(getattr(context, "read_paths", []) or [])
    if not paths:
        return None
    # read_paths is a set; order by mtime so "recent" means recent.
    def _mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    paths.sort(key=_mtime, reverse=True)

    sections: list[str] = []
    spent = 0
    for path in paths[:max_files]:
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        est = max(1, int(len(body) / max(1.0, chars_per_token)))
        if est > per_file_tokens or spent + est > token_budget:
            sections.append(f"### {path}\n(too large to re-attach; read it if you need it)")
            continue
        spent += est
        sections.append(f"### {path}\n{body}")
    if not sections:
        return None
    return {
        "role": "user",
        "content": (
            "<post-compaction-files>\n"
            "Current contents of the files that were open before the summary "
            "above. These are read fresh from disk, so they already include "
            "your edits. Do not re-read them unless you change them.\n\n"
            + "\n\n".join(sections)
            + "\n</post-compaction-files>"
        ),
    }

# Tool results are the bulk of a long session and the cheapest thing to drop:
# the assistant's own reasoning about them survives in its messages, and the
# file can always be read again. Clearing them needs no model call, so unlike a
# summary it cannot fail at the exact moment the context is too big to send.
def microcompact(
    messages: list[dict],
    *,
    keep_recent: int = 20,
    min_chars: int = 400,
) -> tuple[int, int]:
    """Blank the bodies of old tool results in place.

    Leaves the newest ``keep_recent`` tool results untouched (the model is
    usually still working with those) and ignores results already small enough
    that clearing them buys nothing. Returns (results_cleared, chars_reclaimed).
    """
    indexes = [
        i for i, m in enumerate(messages)
        if isinstance(m, dict)
        and m.get("role") == "tool"
        and isinstance(m.get("content"), str)
        and m["content"] != MICROCOMPACT_CLEARED_MESSAGE
    ]
    if keep_recent > 0:
        indexes = indexes[:-keep_recent] if keep_recent < len(indexes) else []
    cleared = 0
    reclaimed = 0
    for i in indexes:
        body = messages[i]["content"]
        if len(body) < min_chars:
            continue
        messages[i] = {**messages[i], "content": MICROCOMPACT_CLEARED_MESSAGE}
        cleared += 1
        reclaimed += len(body) - len(MICROCOMPACT_CLEARED_MESSAGE)
    return cleared, reclaimed


def _is_context_overflow_error(exc: BaseException) -> bool:
    if not isinstance(exc, ai.ProviderAPIError):
        return False
    fields: list[str] = [str(exc)]
    for attr in ("code", "error_type", "status_code", "body"):
        value = getattr(exc, attr, None)
        if value is not None:
            fields.append(str(value))
    haystack = " ".join(fields).lower()
    if "413" in haystack:
        return True
    return any(needle in haystack for needle in _CONTEXT_OVERFLOW_NEEDLES)


def _backoff(attempt: int) -> float:
    """Exponential with jitter: 1s, 2s, 4s ... capped."""
    base = min(2 ** attempt, 16)
    return base + random.uniform(0, 1)


# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------

@dataclass
class Telemetry:
    debug_log: object  # Path | None — typed loosely to avoid import cycles
    trace_sink: object = None  # a .write()-able sink for the full request trace, or None
    transcript_log: object = None  # visible transcript sink; never raises

    def event(self, kind: str, **fields: Any) -> None:
        if not self.debug_log:
            return
        rec = {"ts": time.time(), "kind": kind, **fields}
        try:
            with open(self.debug_log, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass  # telemetry must never break the loop


@dataclass
class _PendingToolCall:
    id: str
    name: str = ""
    arg_chunks: list[str] = field(default_factory=list)

    def arguments(self) -> str:
        return "".join(self.arg_chunks)


def _incomplete_has_dangling_tool_args(pending_calls: list[_PendingToolCall]) -> bool:
    return any(not tool_args.is_json_object(pc.arguments()) for pc in pending_calls)


def _truncated_tool_call_notice(reason: str, pending_calls: list[_PendingToolCall]) -> str:
    names = sorted({pc.name or "unknown" for pc in pending_calls})
    joined = ", ".join(names)
    suffix = f" for {joined}" if joined else ""
    return (
        f"Response incomplete ({reason}); dropped truncated tool call arguments{suffix}. "
        "Retry the request or ask for the change in smaller chunks."
    )


_IMAGE_RESULT_PREFIX = "IMAGE_RESULT\t"


def _history_tool_result_message(pc: _PendingToolCall, result: str) -> list[dict]:
    """Persistence form of a tool result. Image markers collapse to their text stub so the
    base64 payload is sent once (the turn it is read) and never re-billed on history replay."""
    if not result.startswith(_IMAGE_RESULT_PREFIX):
        return [{"role": "tool", "tool_call_id": pc.id, "name": pc.name, "content": result}]
    parts = result.split("\t", 3)
    stub = parts[3] if len(parts) == 4 else "VISUAL_FILE (image omitted from history)"
    return [{"role": "tool", "tool_call_id": pc.id, "name": pc.name, "content": stub}]


# --------------------------------------------------------------------------
# Pretty tool trace
# --------------------------------------------------------------------------

# Per-tool arg display: which args to show, in what order, how to truncate.
# None = show all args as a compact one-liner.

_TOOL_DISPLAY: dict[str, list[tuple[str, int]]] = {
    # (arg_name, max_len) — max_len 0 means hide it. Keyed by CANONICAL tool names only;
    # _dispatch resolves aliases to the canonical name before formatting.
    "shell":          [("command", 120), ("cwd", 40), ("description", 60)],
    "read":           [("file_path", 80), ("path", 80), ("range", 0)],
    "write":          [("file_path", 80), ("path", 80), ("content", 0), ("overwrite", 0)],
    "patch":          [("file_path", 80), ("path", 80), ("old_string", 30), ("new_string", 0)],
    "multi_patch":    [("file_path", 80), ("path", 80), ("edits", 0)],
    "fs_search":      [("pattern", 60), ("path", 40), ("output_mode", 20)],
    "fetch":          [("url", 100), ("raw", 0)],
}


def _pretty_args(name: str, args: dict) -> str:
    """Format tool args for the trace line. Tool-specific pretty printing."""
    display = _TOOL_DISPLAY.get(name)
    if display is None:
        # Unknown tool — compact JSON dump, truncated
        return _short_default(args)

    parts: list[str] = []
    for key, maxlen in display:
        if maxlen == 0:
            continue
        val = args.get(key)
        if val is None:
            continue
        s = str(val)
        if len(s) > maxlen:
            s = s[:maxlen - 3] + "..."
        parts.append(f"{key}={C.CYAN}{s}{C.MAGENTA}")

    if not parts:
        return ""
    return " ".join(parts)


def _short_default(args: dict) -> str:
    """Fallback compact JSON for unknown tools."""
    try:
        s = json.dumps(args, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= 80 else s[:77] + "..."


def _repair_jsonish(raw: str) -> dict:
    return tool_args.repair_jsonish(raw)


def _canonical_tool_args(raw: str) -> str:
    """Return tool-call args as a string the `ai` SDK will accept verbatim.

    The model occasionally emits args that are not strictly valid JSON (a
    trailing comma, an unclosed brace, a double-encoded string) — typically on
    large `write`/`patch` content payloads. `_dispatch` repairs these for
    execution via `_repair_jsonish`, but the *history* we resend each turn
    carries the raw string. The SDK's integrity pass (`ai.types.integrity`)
    re-validates every prior tool call with `json.loads` and **blanks**
    unparseable args to ``"{}"`` (logging "invalid-tool-args"), so the model
    sees its own prior call as empty and flails.

    To keep history and execution consistent we return the raw bytes untouched
    only when they are already a JSON *object*. Otherwise — unparseable args, or
    args that are valid JSON of the wrong shape (a double-encoded string wrapping
    the real object) — we substitute the repaired, canonical object so history
    matches what executed. If even the repair fails, the raw string is returned
    unchanged (the SDK will blank it — nothing we can recover there).
    """
    return tool_args.canonical_tool_args(raw)


def _sanitize_assistant_message(msg: ai.messages.Message) -> ai.messages.Message:
    """Repair raw tool-call args inside an SDK assistant message before it is
    resent as history, so the SDK's integrity pass does not blank them to
    ``{}``. Returns the message unchanged when nothing needs repair (frozen
    pydantic models require a copy to mutate)."""
    new_parts: list[Any] = []
    changed = False
    for part in msg.parts:
        if isinstance(part, ai.types.messages.ToolCallPart):
            fixed = _canonical_tool_args(part.tool_args)
            if fixed != part.tool_args:
                part = part.model_copy(update={"tool_args": fixed})
                changed = True
        new_parts.append(part)
    if not changed:
        return msg
    return msg.model_copy(update={"parts": new_parts})


@dataclass
class ToolErrorTracker:
    limit: int = 3
    errors: dict[str, int] = field(default_factory=dict)
    # Under the non-blocking supervisor a turn's leaf tools dispatch in an
    # executor thread while its fan-out calls run as coroutines on the loop; both
    # record into this tracker, so guard the counter against that cross-thread
    # interleave.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(self, tool_name: str, result: str) -> str:
        with self._lock:
            if not result.startswith("ERROR"):
                self.errors.pop(tool_name, None)
                return result
            count = self.errors.get(tool_name, 0) + 1
            self.errors[tool_name] = count
            attempts_left = max(self.limit - count, 0)
            return f"{result}\n<retry>attempts_left={attempts_left}, allowed_max_attempts={self.limit}</retry>"

    def limit_reached(self) -> bool:
        with self._lock:
            return any(count >= self.limit for count in self.errors.values())


# --------------------------------------------------------------------------
# Tool dispatch
# --------------------------------------------------------------------------

def _cap_result(result: str, cap_bytes: int, inline_cap: int | None = None) -> str:
    """Spill-then-clip a tool result.

    inline_cap (limits.max_tool_result_inline_bytes) sends anything larger to a
    file first, so what the model loses is only its position in the text, not
    the text. cap_bytes is the hard backstop after that.

    Original contract below: clip to the byte cap, leaving a visible marker when the slice
    actually shortens it — same wording the subagent layer uses (meta.py) so a
    truncated leaf result never looks like the tool simply stopped early. A cap of
    0 or less means unlimited (matches the settings convention)."""
    if inline_cap is None:
        # Read from the active context rather than threading a fifth argument
        # through four dispatch signatures; subagent contexts inherit it.
        inline_cap = int(getattr(T.DEFAULT_CONTEXT, "max_tool_result_inline_bytes", 0) or 0)
    if inline_cap > 0:
        result = spill_oversized_result(result, inline_cap)
    if cap_bytes > 0 and len(result) > cap_bytes:
        return result[:cap_bytes] + f"\n[truncated: limits.max_tool_result_bytes ({cap_bytes}) reached]"
    return result


def spill_oversized_result(result: str, inline_cap: int, *, spill_dir: Path | None = None) -> str:
    """Write an oversized result to disk and return a preview plus the path.

    Clipping throws the tail away — the model cannot get it back and often
    does not know it existed. Spilling keeps every byte addressable: the model
    reads the file with an offset if it needs the rest. 0 or less disables.
    """
    if inline_cap <= 0 or len(result) <= inline_cap:
        return result
    target_dir = spill_dir or (Path(os.path.expanduser("~")) / "oldinbox" / "js-tool-results")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(result.encode("utf-8", "replace")).hexdigest()[:16]
        path = target_dir / f"result-{digest}.txt"
        if not path.exists():
            path.write_text(result, encoding="utf-8")
    except OSError:
        return result  # cannot spill -> the byte cap downstream still applies
    head = result[: max(0, inline_cap // 2)]
    return (
        f"{head}\n\n[result was {len(result)} bytes, over "
        f"limits.max_tool_result_inline_bytes ({inline_cap}); the full text is at "
        f"{path} — read it with start_line/end_line for the rest]"
    )


def _fair_share_ceiling(sizes: list[int], budget: int) -> int:
    """Largest per-result allowance L where sum(min(size, L)) <= budget.

    Water-filling: small results are paid in full and never clipped, the budget
    left over is split evenly among whatever is still oversized. Returns -1 when
    everything already fits.
    """
    remaining = budget
    for i, size in enumerate(sorted(sizes)):
        share = remaining // (len(sizes) - i)
        if size > share:
            return share
        remaining -= size
    return -1


def _cap_batch_results(results: list[str], cap_bytes: int) -> list[str]:
    """Clip one turn's batch of tool results down to an aggregate budget.

    The per-result cap (max_tool_result_bytes) can't stop N parallel calls from
    each returning a legal 256 KB and burying the turn under 2 MB, so the batch
    gets its own ceiling. Biggest results lose bytes first — a turn of one fat
    result and six small ones only clips the fat one. 0 or less = unlimited.
    """
    if cap_bytes <= 0:
        return results
    sizes = [len(r) for r in results]
    if sum(sizes) <= cap_bytes:
        return results
    marker = f"\n[truncated: limits.max_tool_results_per_turn_bytes ({cap_bytes}) reached]"
    allowance = _fair_share_ceiling(sizes, cap_bytes)
    if allowance < 0:
        return results
    keep = max(0, allowance - len(marker))
    return [r if len(r) <= allowance else r[:keep] + marker for r in results]


def _dispatch(name: str, raw_args: str, telemetry: Telemetry,
              cap_bytes: int, trace: bool = False,
              error_tracker: ToolErrorTracker | None = None,
              registry: ToolRegistry | None = None,
              tool_context: ToolContext | None = None) -> tuple[dict, str]:
    """Parse + execute one tool call. Returns (parsed_args, result_string)."""
    try:
        args = _repair_jsonish(raw_args)
    except ValueError as e:
        if trace:
            print(f"  {C.MAGENTA}▸ {name}{C.RESET} {C.ORANGE}<malformed args>{C.RESET}", flush=True)
        telemetry.event("tool_error", tool=name, error=f"argparse: {e}")
        result = f"ERROR: could not parse arguments for {name}: {e}"
        if error_tracker is not None:
            result = error_tracker.record(name, result)
        return {}, result

    active_registry = registry or T._REGISTRY
    context = tool_context or T.DEFAULT_CONTEXT
    tool = active_registry.resolve(name)
    trace_name = tool.name if tool is not None else name
    if trace:
        pretty = _pretty_args(trace_name, args)
        if pretty:
            print(f"  {C.MAGENTA}▸ {trace_name}{C.RESET} {pretty}{C.RESET}", flush=True)
        else:
            print(f"  {C.MAGENTA}▸ {trace_name}{C.RESET}", flush=True)
    if tool is None:
        telemetry.event("tool_unknown", tool=name, args=args)
        result = f"ERROR: no tool named {name}; use {active_registry.names()}"
        if error_tracker is not None:
            result = error_tracker.record(name, result)
        return args, _cap_result(result, cap_bytes)

    started = time.time()
    try:
        result = call_tool(tool, args, context)
        telemetry.event("tool_ok", tool=tool.name, latency_ms=int((time.time() - started) * 1000))
    except Exception as e:  # noqa: BLE001
        telemetry.event("tool_exception", tool=tool.name,
                        error=f"{type(e).__name__}: {e}",
                        latency_ms=int((time.time() - started) * 1000))
        result = f"ERROR running {tool.name}: {type(e).__name__}: {e}"
    if error_tracker is not None:
        result = error_tracker.record(tool.name, result)
    return args, _cap_result(result, cap_bytes)


def _is_task_call(pc: _PendingToolCall) -> bool:
    return pc.name.lower() == "task"


def _dispatch_tool_calls(
    tool_calls: list[_PendingToolCall],
    telemetry: Telemetry,
    cap_bytes: int,
    trace: bool,
    error_tracker: ToolErrorTracker,
    registry: ToolRegistry,
    tool_context: ToolContext,
) -> list[tuple[_PendingToolCall, dict, str]]:
    """Dispatch one assistant batch with Forge-style task parallelism.

    Forge runs all `task` calls from the same assistant turn concurrently, runs
    non-task tools sequentially, then restores the original result order before
    appending tool messages.
    """
    records: list[tuple[dict, str] | None] = [None] * len(tool_calls)
    task_indices = [idx for idx, pc in enumerate(tool_calls) if _is_task_call(pc)]

    if task_indices:
        with ThreadPoolExecutor(max_workers=len(task_indices), thread_name_prefix="js-runtime-task") as executor:
            futures = {
                idx: executor.submit(
                    _dispatch,
                    tool_calls[idx].name,
                    tool_calls[idx].arguments(),
                    telemetry,
                    cap_bytes,
                    trace,
                    None,
                    registry,
                    tool_context,
                )
                for idx in task_indices
            }
            for idx in task_indices:
                try:
                    args, result = futures[idx].result()
                except Exception as exc:  # noqa: BLE001
                    args = {}
                    result = f"ERROR running task: {type(exc).__name__}: {exc}"
                records[idx] = (args, error_tracker.record("task", result))

    for idx, pc in enumerate(tool_calls):
        if records[idx] is not None:
            continue
        records[idx] = _dispatch(
            pc.name,
            pc.arguments(),
            telemetry,
            cap_bytes,
            trace,
            error_tracker,
            registry,
            tool_context,
        )

    return [
        (pc, args, result)
        for pc, (args, result) in zip(tool_calls, records, strict=True)
    ]


async def _dispatch_fan_out_async(
    pc: _PendingToolCall,
    telemetry: Telemetry,
    cap_bytes: int,
    trace: bool,
    error_tracker: ToolErrorTracker,
    registry: ToolRegistry,
    tool_context: ToolContext,
) -> tuple[_PendingToolCall, dict, str]:
    """Execute ONE fan-out (task / named-agent) tool call by awaiting its child
    turns on the current loop (never a dispatch thread). Mirrors ``_dispatch``'s
    parse/trace/telemetry/error-tracking so a fan-out call is indistinguishable
    from a threaded one to the caller."""
    from .toolkit import meta

    try:
        args = _repair_jsonish(pc.arguments())
    except ValueError as e:
        if trace:
            print(f"  {C.MAGENTA}▸ {pc.name}{C.RESET} {C.ORANGE}<malformed args>{C.RESET}", flush=True)
        telemetry.event("tool_error", tool=pc.name, error=f"argparse: {e}")
        result = f"ERROR: could not parse arguments for {pc.name}: {e}"
        return pc, {}, error_tracker.record(pc.name, result)

    tool = registry.resolve(pc.name)
    trace_name = tool.name if tool is not None else pc.name
    if trace:
        pretty = _pretty_args(trace_name, args)
        line = f"  {C.MAGENTA}▸ {trace_name}{C.RESET} {pretty}{C.RESET}" if pretty else f"  {C.MAGENTA}▸ {trace_name}{C.RESET}"
        print(line, flush=True)
    if tool is None:
        telemetry.event("tool_unknown", tool=pc.name, args=args)
        result = f"ERROR: no tool named {pc.name}; use {registry.names()}"
        return pc, args, error_tracker.record(pc.name, _cap_result(result, cap_bytes))

    started = time.time()
    try:
        result = await meta.dispatch_fan_out_async(tool, args, tool_context)
        telemetry.event("tool_ok", tool=tool.name, latency_ms=int((time.time() - started) * 1000))
    except Exception as e:  # noqa: BLE001
        telemetry.event("tool_exception", tool=tool.name,
                        error=f"{type(e).__name__}: {e}",
                        latency_ms=int((time.time() - started) * 1000))
        result = f"ERROR running {tool.name}: {type(e).__name__}: {e}"
    return pc, args, error_tracker.record(tool.name, _cap_result(result, cap_bytes))


async def _dispatch_batch(
    tool_calls: list[_PendingToolCall],
    telemetry: Telemetry,
    cap_bytes: int,
    trace: bool,
    error_tracker: ToolErrorTracker,
    registry: ToolRegistry,
    tool_context: ToolContext,
    loop: asyncio.AbstractEventLoop,
) -> list[tuple[_PendingToolCall, dict, str]]:
    """Dispatch one assistant batch, keeping the shared dispatch pool free of
    blocked-on-descendant waiters.

    When a non-blocking supervisor is live AND the batch contains fan-out
    (task / named-agent) calls, those run ON THE LOOP as cancelable subagent jobs
    (`_dispatch_fan_out_async`) — so a parent turn awaiting its subtree never
    parks a bounded js-dispatch thread that its own descendants need for their
    leaf tool dispatch (the pool-inversion deadlock). Leaf calls in the same
    batch still run in the executor, concurrently. Without a supervisor (``-p``,
    bench, tests) the whole batch takes the executor path unchanged."""
    from . import supervisor

    fan_out_idx: list[int] = []
    current_supervisor = supervisor.get_current()
    if current_supervisor is not None:
        from .toolkit import meta

        for i, pc in enumerate(tool_calls):
            tool = registry.resolve(pc.name)
            if tool is not None and meta.is_fan_out_handler(tool.handler):
                fan_out_idx.append(i)

    if not fan_out_idx:
        if current_supervisor is None:
            return _dispatch_tool_calls(
                tool_calls, telemetry, cap_bytes, trace, error_tracker, registry, tool_context,
            )
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="js-runtime-leaf") as executor:
            future = executor.submit(
                _dispatch_tool_calls,
                tool_calls, telemetry, cap_bytes, trace, error_tracker, registry, tool_context,
            )
            return await asyncio.wrap_future(future, loop=loop)

    fan_out_set = set(fan_out_idx)
    records: list[tuple[_PendingToolCall, dict, str] | None] = [None] * len(tool_calls)

    leaf_calls = [pc for i, pc in enumerate(tool_calls) if i not in fan_out_set]
    leaf_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="js-runtime-leaf") if leaf_calls else None
    leaf_future = (
        asyncio.wrap_future(
            leaf_executor.submit(
                _dispatch_tool_calls,
                leaf_calls, telemetry, cap_bytes, trace, error_tracker, registry, tool_context,
            ),
            loop=loop,
        )
        if leaf_executor is not None else None
    )
    try:
        fan_out_records = await asyncio.gather(*(
            _dispatch_fan_out_async(tool_calls[i], telemetry, cap_bytes, trace, error_tracker, registry, tool_context)
            for i in fan_out_idx
        ))
        for i, rec in zip(fan_out_idx, fan_out_records):
            records[i] = rec

        if leaf_future is not None:
            leaf_records = iter(await leaf_future)
            for i in range(len(records)):
                if records[i] is None:
                    records[i] = next(leaf_records)
    finally:
        if leaf_executor is not None:
            leaf_executor.shutdown(wait=True)

    return records  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Turn loop
# --------------------------------------------------------------------------

_COMPACTION_HEADINGS = (
    "Goal",
    "Decisions and rationale",
    "Files and code",
    "Commands and outcomes",
    "Errors and fixes",
    "Pending and next step",
)


def _compact_setting(cfg: Config, key: str, default: Any = None) -> Any:
    settings = getattr(cfg, "settings", {}) or {}
    cursor: Any = settings.get("compact", {}) if isinstance(settings, dict) else {}
    return cursor.get(key, default) if isinstance(cursor, dict) else default


def _message_text_for_estimate(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)


def _compact_int_setting(cfg: Config, key: str, default: int, *, max_value: int | None = None) -> int:
    raw = _compact_setting(cfg, key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    if max_value is not None:
        return min(value, max_value)
    return value


def _compact_nonnegative_int_setting(cfg: Config, key: str, default: int, *, max_value: int | None = None) -> int:
    raw = _compact_setting(cfg, key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    if max_value is not None:
        return min(value, max_value)
    return value


def _compact_float_setting(cfg: Config, key: str, default: float) -> float:
    raw = _compact_setting(cfg, key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _compact_model_setting(cfg: Config) -> str:
    raw = _compact_setting(cfg, "model", "same")
    if not isinstance(raw, str):
        return cfg.model
    model = raw.strip()
    if not model or model.lower() == "same":
        return cfg.model
    return model


def _compact_pre_hook_setting(cfg: Config) -> str:
    raw = _compact_setting(cfg, "pre_hook")
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def estimate_messages_tokens(messages: list[dict], chars_per_token: float = 4.0) -> int:
    ratio = chars_per_token if chars_per_token and chars_per_token > 0 else 4.0
    return int(sum(len(_message_text_for_estimate(m)) for m in messages) / ratio)


def _last_user_message_index(messages: list[dict]) -> int | None:
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user":
            return idx
    return None


def _compaction_summary_message(summary: str) -> dict:
    return {"role": "user", "content": f"<compaction-summary>\n{summary}\n</compaction-summary>"}


def _safe_tail_start(messages: list[dict], tail_tokens: int, chars_per_token: float = 4.0) -> int:
    if not messages:
        return 0
    ratio = chars_per_token if chars_per_token and chars_per_token > 0 else 4.0
    budget_chars = int(tail_tokens * ratio)
    total = 0
    start = len(messages)
    for idx in range(len(messages) - 1, -1, -1):
        total += len(_message_text_for_estimate(messages[idx]))
        start = idx
        if total >= budget_chars:
            break
    # Back up so an assistant tool_calls message is never separated from the
    # tool result messages that immediately answer it.
    while start > 0:
        prev = messages[start - 1]
        if prev.get("role") == "assistant" and prev.get("tool_calls"):
            start -= 1
            continue
        if messages[start].get("role") == "tool":
            start -= 1
            continue
        break
    return max(0, start)


def _run_compact_pre_hook(cfg: Config) -> str:
    hook = _compact_pre_hook_setting(cfg)
    if not hook:
        return ""
    shell_path = (
        os.environ.get("COMSPEC", "cmd.exe")
        if sys.platform == "win32"
        else os.environ.get("SHELL", "/bin/sh")
    )
    shell_arg = "/C" if sys.platform == "win32" else "-c"
    cap = int(getattr(cfg, "max_bash_output_bytes", 256 * 1024))
    ceiling = int(getattr(cfg, "max_bash_output_ceiling", 0) or 0)
    if ceiling > 0:
        cap = min(cap, ceiling)
    try:
        result = _run_capped(
            [shell_path, shell_arg, hook],
            timeout=30,
            cwd=str(getattr(cfg, "project_dir", Path.cwd())),
            env=None,
            cap=cap,
        )
    except Exception as exc:  # noqa: BLE001
        return f"WARNING: compact pre_hook failed: {type(exc).__name__}: {exc}"
    if not isinstance(result, CappedProcessResult):
        result = CappedProcessResult(result[0], result[1], result[2])
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.stdout_truncated:
        stdout = stdout.rstrip("\n") + f"\n{truncation_marker(cap)}\n"
    if result.stderr_truncated:
        stderr = stderr.rstrip("\n") + f"\n{truncation_marker(cap)}\n"
    if result.returncode != 0:
        return f"WARNING: compact pre_hook exited {result.returncode}: {(stderr or stdout).strip()}"
    return stdout.strip()


def _summary_prompt(messages: list[dict], focus: str, guidance: str) -> str:
    headings = "\n".join(f"## {h}" for h in _COMPACTION_HEADINGS)
    payload = json.dumps(messages, ensure_ascii=False, indent=2, default=str)
    extra = ""
    if focus:
        extra += f"\nFocus: {focus.strip()}\n"
    if guidance:
        extra += f"\nPre-hook guidance/stdout:\n{guidance}\n"
    return (
        "Summarize this js session for loss-minimized context compaction. "
        "Use exactly these six markdown headings and keep concrete file paths, commands, decisions, errors, and next steps.\n\n"
        f"{headings}\n{extra}\nSession messages JSON:\n{payload}"
    )


def _summarize_for_compaction(cfg: Config, model: str, messages: list[dict], focus: str, guidance: str) -> str:
    prompt = _summary_prompt(messages, focus, guidance)
    route = routing.resolve_model_route(
        model,
        configured_provider_id=cfg.provider_id,
        configured_base_url=cfg.provider_base_url,
        configured_api_key=cfg.provider_api_key,
        configured_headers=getattr(cfg, "provider_headers", None),
        explicit_model=True,
    )
    result = model_client.stream_model(
        model_id=route.model,
        provider_id=route.provider_id,
        provider_base_url=route.base_url,
        provider_api_key=route.api_key,
        messages=[ai.user_message(prompt)],
        tools=None,
        max_output_tokens=_compact_int_setting(cfg, "summary_max_tokens", 4096, max_value=8192),
        reasoning_effort=None,
        on_text=lambda _t: None,
        provider_headers=route.headers,
        provider_extra=_provider_extra_params(cfg),
    )
    text = result.text.strip()
    if not text:
        text = "\n".join(f"## {h}\n(Not captured.)" for h in _COMPACTION_HEADINGS)
    return text


async def _summarize_for_compaction_async(
    cfg: Config,
    model: str,
    messages: list[dict],
    focus: str,
    guidance: str,
) -> str:
    route = routing.resolve_model_route(
        model,
        configured_provider_id=cfg.provider_id,
        configured_base_url=cfg.provider_base_url,
        configured_api_key=cfg.provider_api_key,
        configured_headers=getattr(cfg, "provider_headers", None),
        explicit_model=True,
    )
    # The thing being summarized is, by construction, the part of the history
    # that would not fit — so the summarize call can overflow too. Peel the
    # oldest half and retry rather than failing at the exact moment the only
    # way out of the wall is a summary. Matches the PTL retry loop in Claude
    # Code (compact.ts, MAX_PTL_RETRIES).
    head = list(messages)
    result = None
    for peel in range(_SUMMARY_PEEL_RETRIES + 1):
        try:
            result = await model_client.stream_model_async(
                model_id=route.model,
                provider_id=route.provider_id,
                provider_base_url=route.base_url,
                provider_api_key=route.api_key,
                messages=[ai.user_message(_summary_prompt(head, focus, guidance))],
                tools=None,
                max_output_tokens=_compact_int_setting(cfg, "summary_max_tokens", 4096, max_value=8192),
                reasoning_effort=None,
                on_text=lambda _t: None,
                provider_headers=route.headers,
                provider_extra=_provider_extra_params(cfg),
            )
            break
        except ai.ProviderAPIError as exc:
            if not _is_context_overflow_error(exc) or peel >= _SUMMARY_PEEL_RETRIES or len(head) <= 2:
                raise
            dropped = len(head) // 2
            head = head[dropped:]
            print(
                f"  {C.ORANGE}(summary too large; dropped the oldest {dropped} messages "
                f"and retrying, {peel + 1}/{_SUMMARY_PEEL_RETRIES}){C.RESET}",
                flush=True,
            )
    assert result is not None
    text = result.text.strip()
    if not text:
        text = "\n".join(f"## {h}\n(Not captured.)" for h in _COMPACTION_HEADINGS)
    return text


def _calibrated_chars_per_token(cfg: Config, system: str, messages: list[dict]) -> float:
    """Configured chars_per_token, corrected against real provider counts.

    tail_tokens and min_savings_tokens used the raw 4.0 estimate while the
    trigger that called them used provider tokens. On a tool-heavy session the
    estimator drifts far enough that "keep 16k of tail" kept something quite
    different from 16k.
    """
    configured = _compact_float_setting(cfg, "chars_per_token", 4.0)
    try:
        # The tracker lives on the tool context (set in run_turn_async), not in
        # module scope — reading a global here would silently always miss.
        tracker = getattr(T.DEFAULT_CONTEXT, "context_budget_state", None)
        if not hasattr(tracker, "calibrated_chars_per_token"):
            return configured
        return tracker.calibrated_chars_per_token(messages=messages, system=system)
    except Exception:  # noqa: BLE001 - calibration must never break compaction
        return configured


def compact_messages(
    cfg: Config,
    system: str,
    messages: list[dict],
    *,
    focus: str = "",
    forced: bool = False,
    preserve_from: int | None = None,
) -> str:
    from . import memory as M

    chars_per_token = _calibrated_chars_per_token(cfg, system, messages)
    tail_tokens = _compact_int_setting(cfg, "tail_tokens", 16384)
    min_savings = _compact_int_setting(cfg, "min_savings_tokens", 400)
    original_len = len(messages)
    keep_from = _safe_tail_start(messages, tail_tokens, chars_per_token)
    if preserve_from is not None:
        try:
            keep_from = min(keep_from, max(0, int(preserve_from)))
        except (TypeError, ValueError):
            pass
    original_est = estimate_messages_tokens(messages, chars_per_token)
    tail_est = estimate_messages_tokens(messages[keep_from:], chars_per_token)
    if not forced and original_est - tail_est < min_savings:
        return f"compact skipped: estimated savings {original_est - tail_est} tokens < {min_savings}"
    guidance = _run_compact_pre_hook(cfg)
    compact_model = _compact_model_setting(cfg)
    summary = _summarize_for_compaction(cfg, compact_model, messages[:keep_from], focus, guidance)
    M.append_compaction_mark(cfg.session_file, summary=summary, keep_from=keep_from, forced=forced)
    rehydrated = _post_compact_rehydration(T.DEFAULT_CONTEXT, chars_per_token=chars_per_token)
    messages[:] = [
        _compaction_summary_message(summary),
        *([rehydrated] if rehydrated else []),
        *messages[keep_from:],
    ]
    return f"compacted: kept tail from message {keep_from}/{original_len} using {compact_model}"


async def _compact_messages_async(
    cfg: Config,
    system: str,
    messages: list[dict],
    *,
    focus: str = "",
    forced: bool = False,
    preserve_from: int | None = None,
) -> str:
    from . import memory as M

    chars_per_token = _calibrated_chars_per_token(cfg, system, messages)
    tail_tokens = _compact_int_setting(cfg, "tail_tokens", 16384)
    min_savings = _compact_int_setting(cfg, "min_savings_tokens", 400)
    original_len = len(messages)
    keep_from = _safe_tail_start(messages, tail_tokens, chars_per_token)
    if preserve_from is not None:
        try:
            keep_from = min(keep_from, max(0, int(preserve_from)))
        except (TypeError, ValueError):
            pass
    original_est = estimate_messages_tokens(messages, chars_per_token)
    tail_est = estimate_messages_tokens(messages[keep_from:], chars_per_token)
    if not forced and original_est - tail_est < min_savings:
        return f"compact skipped: estimated savings {original_est - tail_est} tokens < {min_savings}"
    guidance = _run_compact_pre_hook(cfg)
    compact_model = _compact_model_setting(cfg)
    summary = await _summarize_for_compaction_async(
        cfg,
        compact_model,
        messages[:keep_from],
        focus,
        guidance,
    )
    M.append_compaction_mark(cfg.session_file, summary=summary, keep_from=keep_from, forced=forced)
    rehydrated = _post_compact_rehydration(T.DEFAULT_CONTEXT, chars_per_token=chars_per_token)
    messages[:] = [
        _compaction_summary_message(summary),
        *([rehydrated] if rehydrated else []),
        *messages[keep_from:],
    ]
    return f"compacted: kept tail from message {keep_from}/{original_len} using {compact_model}"

async def run_turn_async(cfg: Config, system: str, messages: list[dict],
             telemetry: Telemetry, model_override: str | None = None,
             trace_override: bool | None = None,
             reasoning_effort_override: str | None | object = _UNSET,
             max_output_override: int | None | object = _UNSET,
             tool_registry: ToolRegistry | None = None,
             tool_context: ToolContext | None = None,
             suppress_output: bool = False,
             provider_id_override: str | None = None,
             provider_base_url_override: str | None = None,
             provider_api_key_override: str | None = None,
             sampling: Sampling | None = None,
             call_stats: list[dict] | None = None,
             event_hooks: event_mod.EventHooks | None = None) -> None:
    """One user turn → tool-use loop until the model stops. The real primitive:
    it awaits the model stream and runs tool dispatch in a thread executor, so it
    NEVER blocks the loop — many turns/subagents run concurrently. Mutates
    `messages` in place so the caller can persist new entries.

    Provider overrides let the REPL /prompt mode switch endpoint without
    reloading config; unset values fall back to the Config values. The sync
    ``run_turn`` below wraps this for callers not yet on the async runtime.
    """
    model = model_override or cfg.model
    provider_id = provider_id_override if provider_id_override is not None else cfg.provider_id
    provider_base_url = provider_base_url_override if provider_base_url_override is not None else cfg.provider_base_url
    provider_api_key = provider_api_key_override if provider_api_key_override is not None else cfg.provider_api_key
    effort = cfg.reasoning_effort if reasoning_effort_override is _UNSET else reasoning_effort_override
    max_out = cfg.max_output_tokens if max_output_override is _UNSET else max_output_override
    if max_out is None:
        max_out = _resolve_max_output(model, provider_id)
    ai_convo = model_client.history_to_ai_messages(system, messages)
    error_tracker = ToolErrorTracker()
    base_registry = tool_registry or T._REGISTRY
    alias_map = _resolve_alias_profile(getattr(cfg, "settings", {}) or {}, model, provider_id, base_registry)
    active_context = tool_context or T.DEFAULT_CONTEXT
    # Lazy state belongs to this invocation only. The selected registry remains
    # the authorization boundary; discovery can reveal/load only entries in it.
    active_registry = base_registry.aliased(alias_map).lazy_surface(active_context.cwd)
    active_context.tool_registry = active_registry
    active_context.agent_id = cfg.agent_id
    active_context.max_tool_result_bytes = getattr(cfg, "max_tool_result_bytes", active_context.max_tool_result_bytes)
    active_context.max_bash_output_bytes = getattr(cfg, "max_bash_output_bytes", active_context.max_bash_output_bytes)
    active_context.fetch_timeout_s = getattr(cfg, "fetch_timeout_s", active_context.fetch_timeout_s)
    active_context.max_read_lines = getattr(cfg, "max_read_lines", active_context.max_read_lines)
    active_context.max_read_bytes = getattr(cfg, "max_read_bytes", active_context.max_read_bytes)
    active_context.max_tool_result_inline_bytes = getattr(cfg, "max_tool_result_inline_bytes", active_context.max_tool_result_inline_bytes)
    active_context.max_bash_output_ceiling = getattr(cfg, "max_bash_output_ceiling", active_context.max_bash_output_ceiling)
    install_context_window_overrides(cfg)
    active_context.max_line_chars = getattr(cfg, "max_line_chars", active_context.max_line_chars)
    active_context.jsonl_max_line_chars = getattr(cfg, "jsonl_max_line_chars", active_context.jsonl_max_line_chars)
    active_context.max_file_bytes = getattr(cfg, "max_file_bytes", active_context.max_file_bytes)
    active_context.task_max_depth = getattr(cfg, "task_max_depth", getattr(active_context, "task_max_depth", 2))
    active_context.subagent_max_workers = getattr(cfg, "subagent_max_workers", getattr(active_context, "subagent_max_workers", 8))
    active_context.wiki_vault_lock_timeout_s = getattr(cfg, "wiki_vault_lock_timeout_s", getattr(active_context, "wiki_vault_lock_timeout_s", 30))
    active_context.artifact_dir = getattr(cfg, "artifact_dir", None)
    active_context.artifact_url = getattr(cfg, "artifact_url", None)
    active_context.artifact_bin = getattr(cfg, "artifact_bin", None)
    active_context.last_incomplete_reason = None
    active_context.last_output_tokens = 0
    active_context.last_max_output_tokens = max_out
    active_context.compacted_during_turn = False
    active_context.context_tokens = 0
    active_context.tokens_until_compaction = None
    chars_per_token = _compact_float_setting(cfg, "chars_per_token", 4.0)
    token_state = getattr(active_context, "context_budget_state", None)
    if not isinstance(token_state, context_budget.TokenState):
        token_state = context_budget.TokenState(chars_per_token=chars_per_token)
    else:
        token_state.chars_per_token = chars_per_token
    active_context.context_budget_state = token_state
    _wiki_cfg = (getattr(cfg, "settings", {}) or {}).get("wiki")
    _aliases = _wiki_cfg.get("aliases") if isinstance(_wiki_cfg, dict) else None
    active_context.vault_aliases = _aliases if isinstance(_aliases, dict) else {}
    active_context.vision_enabled = vision_enabled_for_model(model)

    def _emit_event(event: str, **payload: Any) -> list[event_mod.EventHook]:
        if event_hooks is None:
            return []
        emission = event_hooks.emit(event, **payload)
        for result in emission.results:
            if result.error:
                telemetry.event(
                    "event_handler_error",
                    event=emission.event,
                    handler=result.hook.handler,
                    error=result.error,
                )
        return emission.hooks

    def _end_turn(reason: str, **extra: Any) -> None:
        _emit_event("turn_end", reason=reason, model=model, provider_id=provider_id, **extra)

    _emit_event(
        "turn_start",
        model=model,
        provider_id=provider_id,
        message_count=len(messages),
    )

    trace = trace_override if trace_override is not None else cfg.trace
    if trace:
        if provider_id:
            _provider_label = provider_id
            _base = provider_base_url or "provider-default"
        else:
            _provider_label = "ai-sdk"
            if ":" in model:
                _base = model.split(":")[0]
            else:
                _base = "ai-gateway"
        # ctx is the number that decides when compaction fires and how much room
        # is left to work in; max_out only bounds one reply. Showing the second
        # without the first invites reading 128000 as the window.
        _ctx_for_banner = _resolve_context_window(model, provider_id, provider_base_url)
        _bits = [f"model={model}",
                 f"provider={_provider_label}",
                 f"base={_base}",
                 f"ctx={_ctx_for_banner if _ctx_for_banner else 'unknown'}",
                 f"max_out={max_out if max_out is not None else 'provider-default'}"]
        if effort:
            _bits.append(f"effort={effort}")
        _bits.append(f"vision={'on' if active_context.vision_enabled else 'off'}")
        try:
            _ntools = len(active_registry.openai_specs())
        except Exception:  # noqa: BLE001 — registry internals
            _ntools = "?"
        _bits.append(f"tools={_ntools}")
        print(f"  {C.CYAN}▸ run{C.RESET} {C.GREY}{'  '.join(_bits)}{C.RESET}", flush=True)

    # Streaming text: open WHITE once at first chunk, close RESET + newline
    # once after the stream completes. Avoids per-chunk escape wrapping.
    text_started = {"value": False}
    _transcript_log = getattr(telemetry, "transcript_log", None)

    def _muted_transcript_tee():
        mute = getattr(_transcript_log, "mute_tee", None)
        if callable(mute):
            return mute()
        return contextlib.nullcontext()

    def _emit_text(t: str) -> None:
        if not t:
            return
        _emit_event("stream", text=t)
        if suppress_output:
            return
        if _transcript_log is not None:
            write_chunk = getattr(_transcript_log, "write_assistant_chunk", None)
            if callable(write_chunk):
                write_chunk(t)
        if not text_started["value"]:
            with _muted_transcript_tee():
                sys.stdout.write(C.WHITE)
            text_started["value"] = True
        with _muted_transcript_tee():
            sys.stdout.write(t)
            sys.stdout.flush()

    def _close_text() -> None:
        if suppress_output:
            text_started["value"] = False
            return
        if text_started["value"]:
            if _transcript_log is not None:
                end_stream = getattr(_transcript_log, "end_assistant_stream", None)
                if callable(end_stream):
                    end_stream()
            with _muted_transcript_tee():
                sys.stdout.write(C.RESET + "\n")
                sys.stdout.flush()
            text_started["value"] = False

    # Full request trace: dump system prompt + full tool schemas once (first
    # model call), then only the newly-sent messages each call. This goes ONLY to
    # the trace sink (autolog file / --debug-file), never to stdout — decoupled
    # from the concise `trace` flag that drives the `▸` lines on the terminal.
    _trace_sink = getattr(telemetry, "trace_sink", None)
    _trace_req = {"sent": 0, "schemas": True}

    active_compact_cfg = replace(
        cfg,
        model=model,
        provider_id=provider_id,
        provider_base_url=provider_base_url,
        provider_api_key=provider_api_key,
    )

    def _budget_context_window() -> int:
        inferred = _resolve_context_window(model, provider_id, provider_base_url)
        window = _compact_int_setting(active_compact_cfg, "context_window", inferred or 0)
        if window > 0:
            return window
        # Nothing known about this model. context_window is an override that
        # applies to every model at once, so pinning it to cover one unknown
        # model throws away the real window of every model that IS known;
        # context_window_fallback only lands here, where there is no metadata.
        return _compact_int_setting(active_compact_cfg, "context_window_fallback", 0)

    def _budget_buffer_tokens() -> int:
        return _compact_nonnegative_int_setting(active_compact_cfg, "buffer_tokens", 4096)

    def _active_preserve_from() -> int | None:
        return _last_user_message_index(messages)

    async def _maybe_compact_request_for_budget(
        *,
        phase: str,
        specs: list[dict],
        force: bool = False,
    ) -> bool:
        nonlocal ai_convo
        if not force and not bool(_compact_setting(active_compact_cfg, "auto", True)):
            return False
        context_window = _budget_context_window()
        if context_window <= 0 and not force:
            return False
        preserve_from = _active_preserve_from()
        ai_tools_for_budget = model_client.tool_specs_to_ai_tools(specs) if specs else None
        status = token_state.budget_status(
            system=system,
            messages=messages,
            tools=ai_tools_for_budget,
            context_window=context_window if context_window > 0 else None,
            output_reserve_tokens=max_out or 0,
            buffer_tokens=_budget_buffer_tokens(),
        )
        active_context.context_tokens = status.current_context_tokens
        active_context.tokens_until_compaction = status.tokens_until_compaction
        telemetry.event(
            "context_budget",
            phase=phase,
            context_tokens=status.current_context_tokens,
            context_window=status.context_window,
            effective_input_limit=status.effective_input_limit,
            tokens_until_compaction=status.tokens_until_compaction,
            used_provider_usage=status.used_provider_usage,
        )
        if not (force or status.should_compact):
            return False
        if preserve_from is None or preserve_from <= 0:
            telemetry.event("context_compaction_skipped", phase=phase, reason="no_compactable_prefix")
            return False
        try:
            result = await _compact_messages_async(
                active_compact_cfg,
                system,
                messages,
                focus=f"{phase} context budget",
                forced=True,
                preserve_from=preserve_from,
            )
        except Exception as exc:  # noqa: BLE001
            telemetry.event(
                "context_compaction_failed",
                phase=phase,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False
        if not result.startswith("compacted:"):
            telemetry.event("context_compaction_skipped", phase=phase, reason=result)
            return False
        token_state.reset()
        ai_convo = model_client.history_to_ai_messages(system, messages)
        _trace_req["sent"] = 0
        _trace_req["schemas"] = True
        active_context.compacted_during_turn = True
        telemetry.event("context_compacted", phase=phase, result=result)
        return True

    try:
        durable_side_effects_started = False
        overflow_recovered = 0
        for _ in range(cfg.max_tool_iterations):
            # --- One model call with retry on retriable transport errors ---
            text = ""
            pending_calls: list[_PendingToolCall] = []
            finish: str | None = None
            reasoning = ""
            result: model_client.ModelStreamResult | None = None
            usage = None
            provider_metadata: dict[str, Any] | None = None
            incomplete_reason: str | None = None
            budget_checked = False
            for attempt in range(3):
                t0 = time.time()
                try:
                    specs = _aliased_tool_specs(active_registry.openai_specs(), alias_map)
                    if not budget_checked:
                        await _maybe_compact_request_for_budget(
                            phase="midturn" if durable_side_effects_started else "preflight",
                            specs=specs,
                        )
                        budget_checked = True
                    ai_tools = model_client.tool_specs_to_ai_tools(specs) if specs else None
                    _emit_event(
                        "prompt",
                        model=model,
                        provider_id=provider_id,
                        message_count=len(ai_convo),
                        tool_count=len(specs),
                    )
                    _res = model_client.stream_model_async(
                        model_id=model,
                        provider_id=provider_id,
                        provider_base_url=provider_base_url,
                        provider_api_key=provider_api_key,
                        messages=ai_convo,
                        tools=ai_tools,
                        max_output_tokens=max_out,
                        reasoning_effort=effort,
                        on_text=_emit_text,
                        provider_headers=getattr(cfg, "provider_headers", None),
                        provider_extra=_provider_extra_params(cfg),
                        sampling=sampling,
                        trace_request=_trace_sink is not None,
                        trace_sink=_trace_sink,
                        trace_request_schemas=_trace_req["schemas"],
                        trace_request_from=_trace_req["sent"],
                    )
                    if _trace_sink is not None:
                        _trace_req["sent"] = len(ai_convo)
                        _trace_req["schemas"] = False
                    # Await the native async primitive; tolerate a sync override (a
                    # test stub patched onto stream_model_async that returns a result
                    # directly) so the seam accepts either shape.
                    result = await _res if inspect.isawaitable(_res) else _res
                    _close_text()
                    text = result.text
                    pending_calls = [
                        _PendingToolCall(id=call.id, name=call.name, arg_chunks=[call.arguments])
                        for call in result.tool_calls
                    ]
                    finish = result.finish_reason
                    provider_metadata = (
                        getattr(result, "provider_metadata", None)
                        or getattr(result.assistant_message, "provider_metadata", None)
                    )
                    incomplete_reason = (
                        getattr(result, "incomplete_reason", None)
                        or model_client.incomplete_reason_from_metadata(provider_metadata)
                    )
                    if incomplete_reason:
                        finish = model_client.incomplete_finish_reason(incomplete_reason)
                    reasoning = result.reasoning
                    usage = result.usage
                    active_context.last_prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
                    active_context.last_cached_tokens = int(getattr(usage, "cache_read_tokens", 0) or 0) if usage else 0
                    active_context.last_incomplete_reason = incomplete_reason
                    active_context.last_max_output_tokens = max_out
                    telemetry.event("turn_complete", model=model,
                                    latency_ms=int((time.time() - t0) * 1000),
                                    finish_reason=finish, n_tool_calls=len(pending_calls),
                                    incomplete_reason=incomplete_reason,
                                    prompt_tokens=active_context.last_prompt_tokens,
                                    cached_tokens=active_context.last_cached_tokens)
                    _out_tok = 0
                    if usage:
                        _out_tok = int(getattr(usage, "output_tokens", 0)
                                       or getattr(usage, "completion_tokens", 0) or 0)
                    active_context.last_output_tokens = _out_tok
                    if call_stats is not None:
                        # Stream-isolated numbers (model_client clocks `ai.stream` itself,
                        # free of run_turn's setup/bookkeeping) for honest tok/s and TTFT.
                        _stream_s = result.elapsed_s or (time.time() - t0)
                        call_stats.append({
                            "ttft_s": result.first_token_s,
                            "stream_s": result.elapsed_s,
                            "output_tokens": _out_tok,
                            "prompt_tokens": active_context.last_prompt_tokens,
                            "cached_tokens": active_context.last_cached_tokens,
                            "tok_per_s": (_out_tok / _stream_s) if _stream_s > 0 else 0.0,
                            "finish_reason": finish,
                            "n_tool_calls": len(pending_calls),
                        })
                    if trace:
                        _elapsed = time.time() - t0
                        _tps = (_out_tok / _elapsed) if _elapsed > 0 else 0.0
                        _cache = ""
                        if active_context.last_prompt_tokens > 0 and active_context.last_cached_tokens > 0:
                            _pct = 100.0 * active_context.last_cached_tokens / active_context.last_prompt_tokens
                            _cache = f"  cache {_pct:.0f}%"
                        _ttft = f"  ttft {int(result.first_token_s * 1000)}ms" if result.first_token_s is not None else ""
                        print(f"  {C.GREY}▸ {int(_elapsed * 1000)}ms  "
                              f"finish={finish}  tool_calls={len(pending_calls)}  "
                              f"{_out_tok} tok  {_tps:.1f} tok/s{_ttft}{_cache}{C.RESET}", flush=True)
                    break
                except ai.ProviderAPIError as e:
                    # Terminate any partially streamed text (RESET + newline) before
                    # we retry or abort, so the next attempt's output doesn't
                    # concatenate onto the truncated first attempt with color still open.
                    _close_text()
                    if (
                        _is_context_overflow_error(e)
                        and not durable_side_effects_started
                        and overflow_recovered < _OVERFLOW_RECOVERY_ROUNDS
                    ):
                        overflow_recovered += 1
                        telemetry.event(
                            "context_overflow_error",
                            model=model,
                            error=f"{type(e).__name__}: {e}",
                            attempt=attempt,
                            round=overflow_recovered,
                        )
                        # Round 1 clears old tool bodies — no model call, so it
                        # cannot fail the way a summary can. Only if that is not
                        # enough do we pay for a summary, and each later round
                        # keeps fewer recent results.
                        cleared, reclaimed = microcompact(
                            messages, keep_recent=max(0, 20 // overflow_recovered)
                        )
                        if cleared:
                            print(f"  {C.ORANGE}(context overflow; cleared {cleared} old tool "
                                  f"results, ~{reclaimed // 1000}k chars){C.RESET}", flush=True)
                            token_state.reset()
                            ai_convo = model_client.history_to_ai_messages(system, messages)
                            _trace_req["sent"] = 0
                            _trace_req["schemas"] = True
                            active_context.compacted_during_turn = True
                            continue
                        compacted = await _maybe_compact_request_for_budget(
                            phase="overflow_recovery",
                            specs=_aliased_tool_specs(active_registry.openai_specs(), alias_map),
                            force=True,
                        )
                        if compacted:
                            continue
                    if e.is_retryable:
                        telemetry.event("retriable_error", model=model,
                                        error=f"{type(e).__name__}: {e}", attempt=attempt)
                        if attempt == 2:
                            _emit_event("error", error=f"{type(e).__name__}: {e}", retryable=True)
                            _end_turn("error")
                            raise
                        await asyncio.sleep(_backoff(attempt))
                    else:
                        telemetry.event("fatal_error", model=model,
                                        error=f"{type(e).__name__}: {e}")
                        _emit_event("error", error=f"{type(e).__name__}: {e}", retryable=False)
                        _end_turn("error")
                        raise
                except (ai.ConfigurationError, ai.InstallationError, ai.UnsupportedProviderError, ValueError) as e:
                    _close_text()
                    telemetry.event("fatal_error", model=model,
                                    error=f"{type(e).__name__}: {e}")
                    _emit_event("error", error=f"{type(e).__name__}: {e}", retryable=False)
                    _end_turn("error")
                    raise
            else:
                print(f"  {C.ORANGE}▸ tool-loop retry budget exhausted{C.RESET}")
                _end_turn("retry_budget_exhausted")
                return

            assistant_message_override: ai.messages.Message | None = None
            if incomplete_reason and pending_calls and _incomplete_has_dangling_tool_args(pending_calls):
                notice = _truncated_tool_call_notice(incomplete_reason, pending_calls)
                telemetry.event(
                    "tool_call_dropped",
                    reason="incomplete_truncated_args",
                    incomplete_reason=incomplete_reason,
                    n_tool_calls=len(pending_calls),
                    tools=[pc.name for pc in pending_calls],
                )
                if not suppress_output:
                    _emit_text(("\n\n" if text else "") + notice)
                    _close_text()
                text = f"{text}\n\n{notice}" if text else notice
                pending_calls = []
                assistant_message_override = ai.assistant_message(text)

            # Freeze dispatch authorization to the schemas this model call saw.
            # Discovery may mutate the turn surface while this batch runs, but a
            # newly loaded tool is callable only after its schema is emitted on
            # the next model iteration.
            dispatch_registry = active_registry.dispatch_registry()

            # --- Record the assistant turn ---
            assistant_record: dict = {"role": "assistant", "content": text}
            history_assistant_record: dict = {"role": "assistant", "content": text}
            if pending_calls:
                if reasoning:
                    assistant_record["reasoning_content"] = reasoning
                assistant_record["tool_calls"] = [
                    {"id": pc.id, "type": "function",
                     "function": {"name": pc.name, "arguments": _canonical_tool_args(pc.arguments())}}
                    for pc in pending_calls
                ]
                history_assistant_record["tool_calls"] = [
                    {"id": pc.id, "type": "function",
                     "function": {"name": _canonical_tool_call_name(pc.name, active_registry), "arguments": _canonical_tool_args(pc.arguments())}}
                    for pc in pending_calls
                ]
            if reasoning:
                history_assistant_record["reasoning_content"] = reasoning
            assert result is not None
            if not isinstance(provider_metadata, dict):
                provider_metadata = None
            incomplete_reason = incomplete_reason or model_client.incomplete_reason_from_metadata(provider_metadata)
            if provider_metadata:
                history_assistant_record["provider_metadata"] = provider_metadata
            if incomplete_reason:
                history_assistant_record["incomplete_reason"] = incomplete_reason
            assistant_message = assistant_message_override or result.assistant_message
            if provider_metadata and not getattr(assistant_message, "provider_metadata", None):
                assistant_message = assistant_message.model_copy(update={"provider_metadata": provider_metadata})
            ai_convo.append(_sanitize_assistant_message(assistant_message))
            messages.append(history_assistant_record)
            durable_side_effects_started = True
            token_state.record_provider_usage(
                usage,
                message_count=len(messages),
                messages=messages,
                system=system,
                tools=ai_tools,
            )
            current_tokens, _estimate, used_provider = token_state.current_context_tokens(
                system=system,
                messages=messages,
                tools=ai_tools,
            )
            active_context.context_tokens = current_tokens
            active_context.context_tokens_used_provider_usage = used_provider
            if text:
                payload = {"text": text, "finish_reason": finish}
                if incomplete_reason:
                    payload["incomplete_reason"] = incomplete_reason
                _emit_event("response", **payload)
            if incomplete_reason and not suppress_output:
                print(f"{C.ORANGE}warning: response incomplete ({incomplete_reason}){C.RESET}", file=sys.stderr)

            if not pending_calls:
                if incomplete_reason:
                    _end_turn("incomplete", finish_reason=finish, incomplete_reason=incomplete_reason)
                else:
                    _end_turn("stop")
                return

            # --- Dispatch tools, append result messages ---
            # ai_convo carries the heavy form (image bytes embedded in tool messages) for THIS
            # turn; messages — persisted and replayed on every future turn — carries the
            # dehydrated stub so base64 is billed once.
            for pc in pending_calls:
                _emit_event(
                    "tool_call",
                    id=pc.id,
                    name=_canonical_tool_call_name(pc.name, active_registry),
                    arguments=_canonical_tool_args(pc.arguments()),
                )
            # Tools are sync (subprocess, file I/O); leaf calls fan out to a worker
            # thread so the shared loop stays free while they execute. Fan-out (task /
            # named-agent) calls are awaited ON the loop instead, so a parent turn
            # never parks a dispatch thread its descendants need (see _dispatch_batch).
            dispatch_records = await _dispatch_batch(
                pending_calls,
                telemetry,
                cfg.max_tool_result_bytes,
                trace,
                error_tracker,
                dispatch_registry,
                active_context,
                asyncio.get_running_loop(),
            )
            capped = _cap_batch_results(
                [r for _, _, r in dispatch_records],
                getattr(cfg, "max_tool_results_per_turn_bytes", 0),
            )
            dispatch_records = [
                (pc, args, new_result)
                for (pc, args, _old), new_result in zip(dispatch_records, capped, strict=True)
            ]
            followup = False
            for pc, _args, result_value in dispatch_records:
                canonical_pc = _pending_with_name(pc, _canonical_tool_call_name(pc.name, active_registry))
                _emit_event(
                    "tool_result",
                    id=pc.id,
                    name=canonical_pc.name,
                    result=result_value,
                )
                tool_msgs = model_client.build_tool_result_messages(pc.id, pc.name, result_value)
                ai_convo.extend(tool_msgs)
                messages.extend(_history_tool_result_message(canonical_pc, result_value))
                followup = followup or result_value.startswith("FOLLOWUP_REQUIRED")
            if followup:
                _end_turn("followup_required")
                return
            if error_tracker.limit_reached():
                name, last_error = next(
                    ((_canonical_tool_call_name(pc.name, active_registry), result_value)
                     for pc, _, result_value in reversed(dispatch_records)
                     if result_value.startswith("ERROR")),
                    (dispatch_records[-1][0].name, dispatch_records[-1][2]),
                )
                failure = f"ERROR: tool retry limit reached after {name}\n{last_error}"
                final_error = {"role": "assistant", "content": failure}
                ai_convo.append(ai.messages.Message(role="assistant", parts=[ai.types.messages.TextPart(text=failure)]))
                messages.append(final_error)
                _emit_event("error", error=failure, retryable=False)
                _end_turn("tool_error_limit")
                return

        print(f"  {C.ORANGE}▸ tool-loop hit max iterations ({cfg.max_tool_iterations}){C.RESET}")
        _end_turn("max_iterations")
    except BaseException as _turn_exc:  # noqa: BLE001
        # turn_start is emitted unconditionally and every normal/handled exit
        # already emitted turn_end; only cancellation (CancelledError /
        # KeyboardInterrupt — BaseException, not Exception) reaches here
        # unbalanced, so pair turn_start with a turn_end before propagating.
        if not isinstance(_turn_exc, Exception):
            _close_text()
            _end_turn("cancelled")
        raise


def run_turn(*args, **kwargs) -> None:
    """Sync wrapper over :func:`run_turn_async` — spins a throwaway loop for this
    turn. The OLD blocking path; the non-blocking runtime awaits
    ``run_turn_async`` directly on its shared loop. Kept so the current sync
    callers (cli one-shot/REPL/bench, subagent threads) keep working through the
    transition. `messages` is still mutated in place, so ^C mid-turn preserves
    partial work exactly as before.
    """
    return asyncio.run(run_turn_async(*args, **kwargs))
