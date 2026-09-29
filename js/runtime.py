"""Tool-use loop. Streaming output, typed error handling, telemetry.
Uses ``js.model_client`` for model I/O via the Vercel AI Python SDK (``ai``)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Awaitable, Callable
import asyncio
import contextlib
import functools
import inspect
import json
import hashlib
import itertools
import os
import sys
import threading
from pathlib import Path
import time
from dataclasses import dataclass, field, replace
from typing import Any

from . import events as event_mod
from . import model_client, memory
import ai

from . import colors as C
from . import context_budget
from . import display
from . import jail as _jail
from . import messages as msgs
from .text_bytes import byte_size, byte_prefix, cap_text
from . import model_metadata
from . import paths
from . import providers
from . import settings as _settings
from . import turn_settings as _turn_settings
from . import toolkit as T
from . import tool_args
from . import routing
from . import skills
from . import compaction
from . import stream_transport
from . import usage as usage_mod
from .config import Config, vision_enabled_for_model
from .sampling import Sampling
from .reasoning_display import ReasoningDisplay
from . import reasoning as reasoning_rules
from .toolkit.core import (ToolContext, ToolResult, call_is_read_only, call_scope, call_tool,
                           call_tool_async, registry_scope)
from .toolkit.registry import ToolRegistry
from .turn_budget import TurnBudget, TurnConvo, last_user_message_index as _last_user_message_index
from .turn_surface import SurfaceJournal
from .turn_stream import StreamSink, TurnEvents
from .turn_call import (CallLimits, ModelCaller, ModelReply, ModelRequest, cut_off_by_cap,
                        dangling_arguments)


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
    return _PendingToolCall(
        id=pc.id,
        name=name,
        arg_chunks=list(pc.arg_chunks),
        validation_error=pc.validation_error,
        unavailable=pc.unavailable,
        refused=pc.refused,
    )


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
    overrides = dict(compaction.get(cfg, "context_window_overrides", None) or {})
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


# --------------------------------------------------------------------------
# Error taxonomy
# --------------------------------------------------------------------------

# Retry only ``ai.ProviderAPIError`` where ``exc.is_retryable`` is true.
# All other provider errors are fatal and abort the turn.


def _is_retriable(exc: BaseException) -> bool:
    if isinstance(exc, ai.ProviderAPIError):
        return bool(exc.is_retryable)
    return False


# Sent as a user message after a reply cut off by its output-token cap.
MAX_OUTPUT_RESUME_NUDGE = (
    "Output token limit hit. Resume directly, no apology, no recap of what you were doing. "
    "Pick up mid-thought if that is where the cut happened. Break remaining work into smaller pieces."
)


# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------

# Serializes trace and telemetry writes from the threads of one dispatch batch.
_OUTPUT_LOCK = threading.RLock()


@dataclass
class Telemetry:
    debug_log: object  # Path | None — typed loosely to avoid import cycles
    trace_sink: object = None  # a .write()-able sink for the full request trace, or None
    transcript_log: object = None  # visible transcript sink; never raises
    reasoning_factory: Callable[[int], ReasoningDisplay] | None = None
    # Builds the answer Display for a screen that owns the terminal; None means
    # a Display over sys.stdout.
    display_factory: Callable[[bool], display.Display] | None = None

    def event(self, kind: str, **fields: Any) -> None:
        rec = {"ts": time.time(), "kind": kind, **fields}
        line = json.dumps(rec, default=str) + "\n"
        with _OUTPUT_LOCK:
            if self.trace_sink is not None:
                try:
                    self.trace_sink.write("FLIGHT " + line)
                except OSError as exc:
                    msgs.warn(msgs.FLIGHT_LOG_FAILED, error=exc)
            if not self.debug_log:
                return
            try:
                with open(self.debug_log, "a") as f:
                    f.write(line)
            except OSError:
                pass  # telemetry must never break the loop


@dataclass
class _PendingToolCall:
    id: str
    name: str = ""
    arg_chunks: list[str] = field(default_factory=list)
    validation_error: str | None = None
    unavailable: bool = False
    # A tools.yaml argument ban matched; validation_error is the ERROR line.
    refused: bool = False

    def arguments(self) -> str:
        return "".join(self.arg_chunks)


@dataclass(frozen=True)
class _ToolBatchStats:
    received: int
    retained: int
    duplicate: int
    invalid: int
    capped: int


def _normalize_tool_call_batch(
    calls: list[_PendingToolCall],
    registry: ToolRegistry,
    limit: int,
) -> tuple[list[_PendingToolCall], _ToolBatchStats]:
    """Canonicalize, deduplicate, validate, and cap one assistant batch."""
    retained: list[_PendingToolCall] = []
    seen: set[tuple[str, str]] = set()
    duplicate = invalid = capped = 0
    scheduled_loads = set()
    for call in calls:
        if _canonical_tool_call_name(call.name, registry) == "tool_discovery":
            try:
                args = _repair_jsonish(call.arguments())
            except ValueError:
                continue
            if isinstance(args, dict) and isinstance(args.get("load"), str):
                scheduled_loads.add(args["load"])

    for call in calls:
        tool = registry.resolve(call.name)
        canonical_name = tool.name if tool is not None else _canonical_tool_call_name(
            call.name, registry
        )
        schema = tool.openai_spec()["function"]["parameters"] if tool is not None else None
        validation_error: str | None = None
        try:
            arguments = _repair_jsonish(call.arguments())
        except ValueError as exc:
            arguments = {}
            validation_error = f"could not parse arguments: {exc}"
        else:
            # Before validation, not after: a container the model serialized as a
            # JSON string is the right value in the wrong type, and rejecting it
            # here means the handler never runs (patch's `edits` batch form was
            # unusable for exactly this reason).
            arguments = tool_args.coerce_json_containers(arguments, schema)
        canonical_args = json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        identity = (canonical_name, canonical_args)
        if identity in seen:
            duplicate += 1
            continue
        seen.add(identity)

        if tool is None:
            validation_error = registry.unavailable_error(canonical_name)
            if scheduled_loads & {f"native:{canonical_name}", f"mcp:{canonical_name}"}:
                validation_error += " A load in this same response cannot authorize a sibling call."
        if validation_error is None:
            validation_error = tool_args.schema_error(arguments, schema)
        refused = False
        if validation_error is None and tool is not None:
            refusal = registry.argument_refusal(tool.name, arguments)
            if refusal is not None:
                validation_error, refused = refusal, True
        if validation_error is not None:
            invalid += 1

        if len(retained) >= limit:
            capped += 1
            continue
        retained.append(
            _PendingToolCall(
                id=call.id,
                name=canonical_name,
                arg_chunks=[canonical_args],
                validation_error=validation_error,
                unavailable=tool is None,
                refused=refused,
            )
        )

    return retained, _ToolBatchStats(
        received=len(calls),
        retained=len(retained),
        duplicate=duplicate,
        invalid=invalid,
        capped=capped,
    )


def _assistant_message_with_tool_calls(
    message: ai.messages.Message,
    calls: list[_PendingToolCall],
    *,
    diagnostic_suffix: str = "",
) -> ai.messages.Message:
    """Replace raw SDK tool-call parts with the normalized batch.

    Parts keep their order and the diagnostic text goes before the first call.
    A signed reasoning part that follows a call the batch dropped is left out:
    its signature covers the parts before it.
    """
    kept = {call.id: call for call in calls}
    parts: list[Any] = []
    placed: set[str] = set()
    dropped_call = False
    suffix = diagnostic_suffix

    def _place_suffix() -> None:
        nonlocal suffix
        if suffix:
            parts.append(ai.types.messages.TextPart(text=suffix))
            suffix = ""

    for part in message.parts:
        if isinstance(part, ai.types.messages.ToolCallPart):
            _place_suffix()
            call = kept.get(part.tool_call_id)
            if call is None or call.id in placed:
                dropped_call = True
                continue
            placed.add(call.id)
            parts.append(part.model_copy(update={"tool_name": call.name, "tool_args": call.arguments()}))
        elif isinstance(part, ai.types.messages.ReasoningPart) and part.provider_metadata and dropped_call:
            continue
        else:
            parts.append(part)
    _place_suffix()
    for call in calls:
        if call.id not in placed:
            parts.append(ai.types.messages.ToolCallPart(
                tool_call_id=call.id,
                tool_name=call.name,
                tool_args=call.arguments(),
            ))
    if not parts:
        parts.append(ai.types.messages.TextPart(text=""))
    return message.model_copy(update={"parts": parts})


def _incomplete_has_dangling_tool_args(pending_calls: list[_PendingToolCall]) -> bool:
    return dangling_arguments(pc.arguments() for pc in pending_calls)


def _truncated_tool_call_notice(reason: str, pending_calls: list[_PendingToolCall]) -> str:
    names = sorted({pc.name or "unknown" for pc in pending_calls})
    joined = ", ".join(names)
    suffix = f" for {joined}" if joined else ""
    return (
        f"Response incomplete ({reason}); dropped truncated tool call arguments{suffix}. "
        "Retry the request or ask for the change in smaller chunks."
    )


_IMAGE_RESULT_PREFIX = "IMAGE_RESULT\t"


def _history_tool_result_message(pc: _PendingToolCall, result: Any) -> list[dict]:
    """Persistence form of a tool result. Image markers collapse to their text stub so the
    base64 payload is sent once (the turn it is read) and never re-billed on history replay."""
    if isinstance(result, ToolResult):
        return [{"role": "tool", "tool_call_id": pc.id, "name": pc.name, "content": result.dehydrated()}]
    if not result.startswith(_IMAGE_RESULT_PREFIX):
        return [{"role": "tool", "tool_call_id": pc.id, "name": pc.name, "content": result}]
    parts = result.split("\t", 3)
    stub = parts[3] if len(parts) == 4 else "VISUAL_FILE (image omitted from history)"
    return [{"role": "tool", "tool_call_id": pc.id, "name": pc.name, "content": stub}]


# --------------------------------------------------------------------------
# Tool trace
# --------------------------------------------------------------------------

def _tool_settings(tool_context: ToolContext | None) -> Any:
    """Live settings for the display dials; `/set ui.tools 3` reaches the next exchange."""
    context = tool_context or T.STOCK_CONTEXT
    return getattr(getattr(context, "config", None), "settings", None)


def _show_trace(telemetry: Telemetry, text: str) -> None:
    """Put one piece of a tool exchange on the screen and in the visible transcript."""
    if not text:
        return
    sink = getattr(telemetry, "transcript_log", None)
    mute = getattr(sink, "mute_tee", None)
    # Parallel read-only calls trace from their own threads; one exchange
    # reaches the screen and the transcript whole.
    with _OUTPUT_LOCK:
        with mute() if callable(mute) else contextlib.nullcontext():
            sys.stdout.write(text)
            sys.stdout.flush()
        write_plain = getattr(sink, "write_plain", None)
        if callable(write_plain):
            write_plain(text)


def _trace_call(telemetry: Telemetry, tool_context: ToolContext | None, name: str,
                args: dict | None, *, malformed: bool = False) -> None:
    settings = _tool_settings(tool_context)
    _show_trace(telemetry, display.render_tool_call(
        name, args, display.tools_level(settings),
        preview=display.preview_lines(settings), malformed=malformed,
    ))


def _trace_result(telemetry: Telemetry, tool_context: ToolContext | None, name: str, result: Any,
                  *, args: dict | None = None, with_call: bool = False, malformed: bool = False) -> None:
    """Print the result half of an exchange. `with_call` prints the call header
    in the same write, so exchanges that finish concurrently stay whole."""
    settings = _tool_settings(tool_context)
    level = display.tools_level(settings)
    preview = display.preview_lines(settings)
    call = (
        display.render_tool_call(name, args, level, preview=preview, malformed=malformed)
        if with_call else ""
    )
    _show_trace(telemetry, call + display.render_tool_result(
        name, result, level, preview=preview, args=args,
    ))


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
        if isinstance(result, _jail.Refusal):
            return result
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

def _tool_result_content_size(result: ToolResult) -> int:
    """Measure model-facing payload content without charging block metadata."""
    size = 0
    for block in result.blocks:
        kind = str(block.get("type", "unknown"))
        if kind == "text":
            size += byte_size(str(block.get("text", "")))
        elif kind == "structured":
            size += byte_size(json.dumps(block.get("value"), ensure_ascii=False, separators=(",", ":"), default=str))
        elif kind in {"image", "audio"}:
            size += byte_size(str(block.get("data", "")))
        elif kind == "resource":
            resource = block.get("resource")
            if isinstance(resource, dict):
                payload = resource.get("text", resource.get("blob", ""))
                size += byte_size(str(payload))
        elif kind == "resource_link":
            size += byte_size(str(block.get("name", ""))) + byte_size(str(block.get("uri", "")))
        else:
            size += byte_size(json.dumps(block, ensure_ascii=False, separators=(",", ":"), default=str))
    return size


def _result_size(result: Any) -> int:
    return _tool_result_content_size(result) if isinstance(result, ToolResult) else byte_size(result)


def _dehydrate_capped_result(result: ToolResult, budget: int, marker: str) -> ToolResult:
    # Even a short media placeholder must disclose the payload was removed.
    return ToolResult.text(cap_text(result.dehydrated() + marker, budget, marker))


def _cap_result(result: Any, cap_bytes: int, inline_cap: int | None = None) -> Any:
    """Spill-then-clip a tool result.

    inline_cap (limits.max_tool_result_inline_bytes) sends anything larger to a
    file first, so what the model loses is only its position in the text, not
    the text. cap_bytes is the hard backstop after that.

    Original contract below: clip to the byte cap, leaving a visible marker when the slice
    actually shortens it — same wording the subagent layer uses (meta.py) so a
    truncated leaf result never looks like the tool simply stopped early. A cap of
    0 or less means unlimited (matches the settings convention)."""
    if isinstance(result, ToolResult):
        if cap_bytes > 0 and _tool_result_content_size(result) > cap_bytes:
            marker = f"\n[truncated: limits.max_tool_result_bytes ({cap_bytes}) reached]"
            return _dehydrate_capped_result(result, cap_bytes, marker)
        return result
    if not isinstance(result, str):
        return result
    if inline_cap is None:
        # Read from the active context rather than threading a fifth argument
        # through four dispatch signatures; subagent contexts inherit it.
        inline_cap = int(getattr(T.STOCK_CONTEXT, "max_tool_result_inline_bytes", 0) or 0)
    if inline_cap > 0:
        result = spill_oversized_result(result, inline_cap)
    if cap_bytes > 0:
        return cap_text(result, cap_bytes, f"\n[truncated: limits.max_tool_result_bytes ({cap_bytes}) reached]")
    return result


def spill_oversized_result(
    result: str,
    inline_cap: int,
    *,
    spill_dir: Path | None = None,
    limit_name: str = "limits.max_tool_result_inline_bytes",
    force: bool = False,
) -> str:
    """Write an oversized result to disk and return a preview plus the path.

    Clipping throws the tail away — the model cannot get it back and often
    does not know it existed. Spilling keeps every byte addressable: the notice
    names the `read` call that continues at the byte where the preview stops,
    and, when the text has more than one line, the line-range call too. A
    single-line payload (JSON, say) is reachable only by byte range. 0 or
    less disables.
    """
    if inline_cap <= 0 or (not force and byte_size(result) <= inline_cap):
        return result
    target_dir = spill_dir or paths.tool_results_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(result.encode("utf-8", "replace")).hexdigest()[:16]
        path = target_dir / f"result-{digest}.txt"
        if not path.exists():
            # Two parallel calls can spill the same text; neither may see the
            # other's half-written file.
            partial = target_dir / f".result-{digest}.{threading.get_ident()}.tmp"
            partial.write_bytes(result.encode("utf-8"))
            os.replace(partial, path)
    except OSError:
        return result  # cannot spill -> the byte cap downstream still applies
    total_bytes = byte_size(result)
    line_count = len(result.splitlines())
    multiline = line_count > 1

    def spill_notice(next_byte: int, next_line: int) -> str:
        by_byte = json.dumps({"start_byte": next_byte})
        if multiline:
            by_line = json.dumps({"start_line": next_line})
            how = f"read it on with range {by_byte}, or by line with range {by_line}"
        else:
            how = f"read it on with range {by_byte}; it is one line, so only a byte range splits it"
        return (
            f"[result was {total_bytes} bytes, over {limit_name} ({inline_cap}); "
            f"the full text is at {path} — {how}]"
        )

    # Inline is a spill threshold, not the hard backstop. Keep a usable pointer
    # even when the notice alone exceeds it; the downstream hard cap still wins.
    # The budget is sized with the widest offsets the notice can name.
    widest = spill_notice(total_bytes, line_count + 1)
    head = byte_prefix(result, min(inline_cap // 2, max(0, inline_cap - byte_size(widest) - 2)))
    head_lines = head.splitlines(keepends=True)
    # The line the preview stops inside, or the next one when it ends on a break.
    next_line = len(head_lines) + (1 if head_lines and head_lines[-1] != head_lines[-1].rstrip("\r\n") else 0)
    notice = spill_notice(byte_size(head), max(1, next_line))
    return f"{head}\n\n{notice}" if head else notice


def _reconcile_read_delivery(
    tool_name: str, args: dict, raw: Any, delivered: Any, context: ToolContext, call_id: str,
) -> None:
    """Match read coverage to what the model actually received.

    A clipping cap (inline spill, per-result cap, or the per-turn batch cap) runs
    after the read handler returned the full text and recorded coverage for all
    of it. Without this, patch is authorized to edit lines the model never saw.
    ``call_id`` names the read call whose coverage is narrowed."""
    if tool_name != "read" or not isinstance(raw, str) or not isinstance(delivered, str):
        return
    if delivered == raw:
        return
    raw_path = args.get("file_path") or args.get("path")
    if not isinstance(raw_path, str):
        return
    # A byte-range read has no line-number prefixes to recover what was
    # delivered, so a clipped one keeps none of its own coverage.
    bounds = args.get("range") if isinstance(args.get("range"), dict) else args
    byte_read = bounds.get("start_byte") is not None or bounds.get("end_byte") is not None
    context.record_delivered_read(context.resolve_path(raw_path), raw, "" if byte_read else delivered, call_id)



def _tool_results_in(messages: list[dict]) -> dict[str, str]:
    """The text result each tool call id holds in ``messages``."""
    return {
        message["tool_call_id"]: message["content"]
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "tool"
        and isinstance(message.get("tool_call_id"), str)
        and isinstance(message.get("content"), str)
    }


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


def _cap_batch_results(results: list[Any], cap_bytes: int) -> list[Any]:
    """Clip one turn's batch of tool results down to an aggregate budget.

    The per-result cap (max_tool_result_bytes) can't stop N parallel calls from
    each returning a legal 256 KB and burying the turn under 2 MB, so the batch
    gets its own ceiling. Biggest results lose bytes first — a turn of one fat
    result and six small ones only clips the fat one. 0 or less = unlimited.
    """
    if cap_bytes <= 0:
        return results
    sizes = [_result_size(r) for r in results]
    if sum(sizes) <= cap_bytes:
        return results
    marker = f"\n[truncated: limits.max_tool_results_per_turn_bytes ({cap_bytes}) reached]"
    allowance = _fair_share_ceiling(sizes, cap_bytes)
    if allowance < 0:
        return results
    capped: list[Any] = []
    for result, size in zip(results, sizes, strict=True):
        if size <= allowance:
            capped.append(result)
        elif isinstance(result, ToolResult):
            capped.append(_dehydrate_capped_result(result, allowance, marker))
        else:
            capped.append(cap_text(result, allowance, marker))
    return capped


def _cell_call_observer(telemetry: Telemetry, trace: bool,
                        context: ToolContext | None) -> Callable[..., None]:
    """Trace and log a tool call a kernel cell makes while this call runs,
    the way a direct call is traced and logged."""

    def observe(name: str, args: dict, result: Any, seconds: float, failure: str | None,
                refused: bool = False) -> None:
        latency_ms = int(seconds * 1000)
        if refused:
            telemetry.event("tool_call_refused", tool=name, via="kernel", refusal=result)
        elif failure is None:
            telemetry.event("tool_ok", tool=name, via="kernel", latency_ms=latency_ms)
        else:
            telemetry.event("tool_exception", tool=name, via="kernel", error=failure,
                            latency_ms=latency_ms)
        if trace:
            _trace_result(telemetry, context, name, result, args=args, with_call=True)

    return observe


def _dispatch(name: str, raw_args: str, telemetry: Telemetry,
              cap_bytes: int, trace: bool = False,
              error_tracker: ToolErrorTracker | None = None,
              registry: ToolRegistry | None = None,
              tool_context: ToolContext | None = None,
              trace_together: bool = False,
              call_id: str | None = None) -> tuple[dict, str]:
    """Parse + execute one tool call. Returns (parsed_args, result_string).

    `trace_together` prints the call header with the result instead of before
    the call runs; concurrent calls use it so their exchanges do not interleave.
    `call_id` is the model's id for the call; read coverage is recorded under it."""
    call_id = call_id or f"dispatch-{next(_ANONYMOUS_CALLS)}"
    try:
        args = _repair_jsonish(raw_args)
    except ValueError as e:
        if trace and not trace_together:
            _trace_call(telemetry, tool_context, name, None, malformed=True)
        telemetry.event("tool_error", tool=name, error=f"argparse: {e}")
        result = f"ERROR: could not parse arguments for {name}: {e}"
        if error_tracker is not None:
            result = error_tracker.record(name, result)
        if trace:
            _trace_result(telemetry, tool_context, name, result, with_call=trace_together, malformed=True)
        return {}, result

    active_registry = registry or T.STOCK_REGISTRY
    context = tool_context or T.STOCK_CONTEXT
    tool = active_registry.resolve(name)
    trace_name = tool.name if tool is not None else name
    if trace and not trace_together:
        _trace_call(telemetry, context, trace_name, args)
    if tool is None:
        telemetry.event("tool_unknown", tool=name, args=args)
        result = active_registry.unavailable_error(name)
        capped = _cap_result(result, cap_bytes)
        if trace:
            _trace_result(telemetry, context, trace_name, capped, args=args, with_call=trace_together)
        return args, capped

    started = time.time()
    try:
        with call_scope(call_id), registry_scope(
                active_registry, _cell_call_observer(telemetry, trace, context)):
            result = call_tool(tool, args, context)
        telemetry.event("tool_ok", tool=tool.name, latency_ms=int((time.time() - started) * 1000))
    except Exception as e:  # noqa: BLE001
        telemetry.event("tool_exception", tool=tool.name,
                        error=f"{type(e).__name__}: {e}",
                        latency_ms=int((time.time() - started) * 1000))
        result = _jail.shown(f"ERROR running {tool.name}: {type(e).__name__}: {e}")
    if error_tracker is not None and isinstance(result, str):
        result = error_tracker.record(tool.name, result)
    capped = _cap_result(result, cap_bytes)
    _reconcile_read_delivery(tool.name, args, result, capped, context, call_id)
    if trace:
        _trace_result(telemetry, context, tool.name, capped, args=args, with_call=trace_together)
    return args, capped


def _is_task_call(pc: _PendingToolCall) -> bool:
    return pc.name.lower() == "task"


def _is_read_only_call(pc: _PendingToolCall, registry: ToolRegistry) -> bool:
    """True when ``pc`` names a tool that writes nothing with these arguments.
    A call that cannot be resolved or parsed is not read-only."""
    tool = registry.resolve(pc.name)
    if tool is None or pc.validation_error is not None:
        return False
    try:
        args = _repair_jsonish(pc.arguments())
    except ValueError:
        return False
    return call_is_read_only(tool, args)


def _read_write_groups(indices: list[int], read_only: Callable[[int], bool]) -> list[list[int]]:
    """Split calls, in model order, into the groups a readers-writer lock admits.

    Consecutive read-only calls share a group and run together. Every other
    call is a group of its own: it starts after every call before it has
    finished, and the calls after it wait for it. read, read, patch, read
    gives [read, read], [patch], [read]."""
    groups: list[list[int]] = []
    readers: list[int] = []
    for idx in indices:
        if read_only(idx):
            readers.append(idx)
            continue
        if readers:
            groups.append(readers)
            readers = []
        groups.append([idx])
    if readers:
        groups.append(readers)
    return groups


_ANONYMOUS_CALLS = itertools.count()


@dataclass
class _DispatchProgress:
    """Known outcomes survive cancellation; the stop flag gates queued leaves."""

    stopped: threading.Event = field(default_factory=threading.Event)
    records: dict[str, tuple[_PendingToolCall, dict, Any]] = field(default_factory=dict)
    finished: dict[str, float] = field(default_factory=dict)   # call id -> when its result came

    def record(self, pc: _PendingToolCall, args: dict, result: Any) -> None:
        self.records[pc.id] = (pc, args, result)
        self.finished[pc.id] = time.time()


def _dispatch_tool_calls(
    tool_calls: list[_PendingToolCall],
    telemetry: Telemetry,
    cap_bytes: int,
    trace: bool,
    error_tracker: ToolErrorTracker,
    registry: ToolRegistry,
    tool_context: ToolContext,
    progress: _DispatchProgress | None = None,
    trace_together: bool = False,
) -> list[tuple[_PendingToolCall, dict, str]]:
    """Dispatch one assistant batch.

    All `task` calls from the same assistant turn run concurrently first. The
    other calls then run in model order under a readers-writer rule (see
    _read_write_groups): read-only calls run together, up to
    tool_context.max_parallel_tools at once, and a call that writes runs
    alone. Results come back in the original order. `trace_together` is set
    when other calls run beside this batch; calls that run concurrently always
    trace together.
    """
    records: list[tuple[dict, str] | None] = [None] * len(tool_calls)
    for idx, pc in enumerate(tool_calls):
        if pc.validation_error is None:
            continue
        args = json.loads(pc.arguments())
        telemetry.event("tool_invalid", tool=pc.name, error=pc.validation_error)
        result = pc.validation_error if pc.unavailable or pc.refused else f"ERROR: invalid arguments for {pc.name}: {pc.validation_error}"
        recorded = _cap_result(result, cap_bytes)
        if not pc.unavailable:
            recorded = error_tracker.record(pc.name, recorded)
        # A call rejected before dispatch is still an exchange the model sees.
        # It used to be invisible in the trace from both ends.
        if trace:
            _trace_result(telemetry, tool_context, pc.name, recorded, args=args, with_call=True)
        records[idx] = (args, recorded)
        if progress is not None:
            progress.record(pc, args, recorded)
    task_indices = [
        idx for idx, pc in enumerate(tool_calls)
        if records[idx] is None and _is_task_call(pc)
    ]

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
                    trace_together or len(task_indices) > 1,
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
                if progress is not None:
                    progress.record(tool_calls[idx], *records[idx])

    def stopped() -> bool:
        return progress is not None and progress.stopped.is_set()

    def run_leaf(idx: int, together: bool) -> None:
        # A queued call checks the stop flag as it starts: ^C lets the calls
        # already running finish and starts no new one.
        if stopped():
            return
        pc = tool_calls[idx]
        records[idx] = _dispatch(
            pc.name,
            pc.arguments(),
            telemetry,
            cap_bytes,
            trace,
            error_tracker,
            registry,
            tool_context,
            together,
            pc.id,
        )
        if progress is not None:
            progress.record(pc, *records[idx])

    limit = max(1, int(getattr(tool_context, "max_parallel_tools", 1) or 1))
    leaves = [idx for idx in range(len(tool_calls)) if records[idx] is None]
    groups = _read_write_groups(
        leaves, lambda idx: limit > 1 and _is_read_only_call(tool_calls[idx], registry),
    )
    for group in groups:
        if stopped():
            break
        if len(group) == 1:
            run_leaf(group[0], trace_together)
            continue
        with ThreadPoolExecutor(max_workers=min(limit, len(group)),
                                thread_name_prefix="js-runtime-read") as executor:
            futures = [executor.submit(run_leaf, idx, True) for idx in group]
        for future in futures:
            future.result()

    return [
        (pc, *record)
        for pc, record in zip(tool_calls, records, strict=True)
        if record is not None
    ]


async def _dispatch_fan_out_async(
    pc: _PendingToolCall,
    telemetry: Telemetry,
    cap_bytes: int,
    trace: bool,
    error_tracker: ToolErrorTracker,
    registry: ToolRegistry,
    tool_context: ToolContext,
    trace_together: bool = False,
) -> tuple[_PendingToolCall, dict, str]:
    """Execute ONE fan-out (task / named-agent) tool call by awaiting its child
    turns on the current loop (never a dispatch thread). Mirrors ``_dispatch``'s
    parse/trace/telemetry/error-tracking so a fan-out call is indistinguishable
    from a threaded one to the caller."""
    from .toolkit import meta

    try:
        args = _repair_jsonish(pc.arguments())
    except ValueError as e:
        if trace and not trace_together:
            _trace_call(telemetry, tool_context, pc.name, None, malformed=True)
        telemetry.event("tool_error", tool=pc.name, error=f"argparse: {e}")
        result = f"ERROR: could not parse arguments for {pc.name}: {e}"
        recorded = error_tracker.record(pc.name, result)
        if trace:
            _trace_result(telemetry, tool_context, pc.name, recorded, with_call=trace_together, malformed=True)
        return pc, {}, recorded

    tool = registry.resolve(pc.name)
    trace_name = tool.name if tool is not None else pc.name
    if trace and not trace_together:
        _trace_call(telemetry, tool_context, trace_name, args)
    if tool is None:
        telemetry.event("tool_unknown", tool=pc.name, args=args)
        result = registry.unavailable_error(pc.name)
        recorded = _cap_result(result, cap_bytes)
        if trace:
            _trace_result(telemetry, tool_context, trace_name, recorded, args=args, with_call=trace_together)
        return pc, args, recorded

    started = time.time()
    try:
        result = await meta.dispatch_fan_out_async(tool, args, tool_context)
        telemetry.event("tool_ok", tool=tool.name, latency_ms=int((time.time() - started) * 1000))
    except Exception as e:  # noqa: BLE001
        telemetry.event("tool_exception", tool=tool.name,
                        error=f"{type(e).__name__}: {e}",
                        latency_ms=int((time.time() - started) * 1000))
        result = _jail.shown(f"ERROR running {tool.name}: {type(e).__name__}: {e}")
    recorded = error_tracker.record(tool.name, _cap_result(result, cap_bytes))
    if trace:
        _trace_result(telemetry, tool_context, tool.name, recorded, args=args, with_call=trace_together)
    return pc, args, recorded


async def _dispatch_async_tool(
    pc: _PendingToolCall, telemetry: Telemetry, cap_bytes: int, trace: bool,
    error_tracker: ToolErrorTracker, registry: ToolRegistry, tool_context: ToolContext,
) -> tuple[_PendingToolCall, dict, Any]:
    try:
        args = _repair_jsonish(pc.arguments())
    except ValueError as exc:
        result = error_tracker.record(pc.name, f"ERROR: could not parse arguments for {pc.name}: {exc}")
        if trace:
            _trace_result(telemetry, tool_context, pc.name, result, with_call=True, malformed=True)
        return pc, {}, result
    tool = registry.resolve(pc.name)
    if tool is None:
        result = registry.unavailable_error(pc.name)
        if trace:
            _trace_result(telemetry, tool_context, pc.name, result, args=args, with_call=True)
        return pc, args, result
    if trace:
        _trace_call(telemetry, tool_context, tool.name, args)
    started = time.time()
    try:
        with call_scope(pc.id), registry_scope(
                registry, _cell_call_observer(telemetry, trace, tool_context)):
            result = await call_tool_async(tool, args, tool_context)
        telemetry.event("tool_ok", tool=tool.name, latency_ms=int((time.time() - started) * 1000))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        telemetry.event("tool_exception", tool=tool.name, error=f"{type(exc).__name__}: {exc}")
        result = _jail.shown(f"ERROR running {tool.name}: {type(exc).__name__}: {exc}")
    raw_result = result
    result = _cap_result(result, cap_bytes)
    _reconcile_read_delivery(tool.name, args, raw_result, result, tool_context, pc.id)
    if isinstance(result, str):
        result = error_tracker.record(tool.name, result)
    if trace:
        _trace_result(telemetry, tool_context, tool.name, result, args=args)
    return pc, args, result


def _interrupt_inflight(tool_context: ToolContext) -> None:
    """Stop the external process a cancelled turn's tool call is blocked on.

    A worker thread running a sync tool cannot be cancelled, so a cancelled turn
    leaves the call running to its own deadline. The kernel tool owns a process
    that outlives the call, and a cell left executing there would sit behind the
    next call; interrupting it makes the drain short and the kernel idle. A
    shell call blocked on its command gets that command's process tree killed,
    so ^C ends the turn now rather than when the shell wait runs out.
    """
    from .toolkit import kernel as kernel_tool
    from .toolkit import process_net

    kernel_tool.interrupt_inflight(tool_context)
    process_net.interrupt_inflight(tool_context)


async def _dispatch_batch(
    tool_calls: list[_PendingToolCall],
    telemetry: Telemetry,
    cap_bytes: int,
    trace: bool,
    error_tracker: ToolErrorTracker,
    registry: ToolRegistry,
    tool_context: ToolContext,
    loop: asyncio.AbstractEventLoop,
    progress: _DispatchProgress | None = None,
) -> list[tuple[_PendingToolCall, dict, Any]]:
    """Drain running sync leaves on cancel, cancel async work, retain outcomes.

    Fan-out stays on the event loop to avoid blocking descendant dispatch on
    an ancestor's worker. Mixed async batches retain model order; pure fan-out
    batches run concurrently with their sync leaves, as before.
    """
    from . import supervisor
    from .toolkit import meta

    progress = progress if progress is not None else _DispatchProgress()
    current_supervisor = supervisor.get_current()
    fan_out = set()
    async_leaves = set()
    for i, pc in enumerate(tool_calls):
        tool = registry.resolve(pc.name)
        if tool is None or pc.validation_error is not None:
            continue
        if current_supervisor is not None and meta.is_fan_out_handler(tool.handler):
            fan_out.add(i)
        elif inspect.iscoroutinefunction(tool.handler):
            async_leaves.add(i)

    async def sync_calls(calls: list[_PendingToolCall]) -> None:
        if not calls or progress.stopped.is_set():
            return
        # A running Python worker cannot be cancelled. Keep its future alive,
        # stop its queued calls, and collect its result before the caller saves
        # the interrupted turn. Never block the event loop with shutdown(wait).
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="js-runtime-leaf") as executor:
            future = loop.run_in_executor(
                executor, _dispatch_tool_calls, calls, telemetry, cap_bytes,
                trace, error_tracker, registry, tool_context, progress, concurrent,
            )
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                progress.stopped.set()
                _interrupt_inflight(tool_context)
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                future.result()
                raise

    async def async_call(i: int) -> None:
        if progress.stopped.is_set():
            return
        if i in fan_out:
            record = await _dispatch_fan_out_async(tool_calls[i], telemetry, cap_bytes, trace,
                                                   error_tracker, registry, tool_context, concurrent)
        else:
            record = await _dispatch_async_tool(tool_calls[i], telemetry, cap_bytes, trace,
                                                error_tracker, registry, tool_context)
        progress.record(*record)

    # Pure fan-out batches run every call at once; their exchanges print whole.
    concurrent = bool(fan_out) and not async_leaves and len(tool_calls) > 1

    jobs: list[asyncio.Task] = []
    try:
        if async_leaves:
            pending = []
            for i, pc in enumerate(tool_calls):
                if i in fan_out or i in async_leaves:
                    await sync_calls(pending)
                    pending = []
                    await async_call(i)
                else:
                    pending.append(pc)
            await sync_calls(pending)
        elif fan_out:
            jobs = [asyncio.create_task(async_call(i)) for i in sorted(fan_out)]
            jobs.append(asyncio.create_task(sync_calls(
                [pc for i, pc in enumerate(tool_calls) if i not in fan_out]
            )))
            await asyncio.gather(*jobs)
        else:
            await sync_calls(tool_calls)
    except BaseException:
        progress.stopped.set()
        for job in jobs:
            job.cancel()
        if jobs:
            drain = asyncio.gather(*jobs, return_exceptions=True)
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    continue
        raise
    return [progress.records[pc.id] for pc in tool_calls if pc.id in progress.records]


# --------------------------------------------------------------------------
# Turn loop
# --------------------------------------------------------------------------

def _turn_request(cfg: Config, *, model_override: str | None, provider_id_override: str | None,
                  provider_base_url_override: str | None, provider_api_key_override: str | None,
                  reasoning_effort_override: Any, max_output_override: Any,
                  sampling: Sampling | None) -> ModelRequest:
    """What this turn's model calls send: ``cfg`` with the overrides that are set."""
    model = model_override or cfg.model
    provider_id = provider_id_override if provider_id_override is not None else cfg.provider_id
    max_out = cfg.max_output_tokens if max_output_override is _UNSET else max_output_override
    if max_out is None:
        max_out = model_metadata.resolve_max_output(model, provider_id)
    return ModelRequest(
        model=model,
        provider_id=provider_id,
        base_url=provider_base_url_override if provider_base_url_override is not None else cfg.provider_base_url,
        api_key=provider_api_key_override if provider_api_key_override is not None else cfg.provider_api_key,
        effort=cfg.reasoning_effort if reasoning_effort_override is _UNSET else reasoning_effort_override,
        max_out=max_out,
        thinking_budget=getattr(cfg, "thinking_budget", None),
        headers=getattr(cfg, "provider_headers", None),
        extra=routing.provider_extra_params(cfg),
        sampling=sampling,
        cache_key=_cache_key(cfg),
    )


def _cache_key(cfg: Config) -> str | None:
    """One conversation, one cache key: OpenAI-compatible endpoints use it to
    route a request to the machine already holding that conversation's prefix.
    Keyed on the session rather than the agent so two concurrent sessions do
    not contend for one prefix. A session-less run sends no key and falls back
    to the provider's own longest-prefix matching."""
    session_file = getattr(cfg, "session_file", None)
    if session_file is None or Path(session_file).name in ("", os.devnull, "null"):
        return None
    return f"js-{cfg.agent_id}-{Path(session_file).stem}"


def _prepare_turn_context(context: ToolContext, cfg: Config, request: ModelRequest,
                          registry: ToolRegistry, event_hooks: Any) -> context_budget.TokenState:
    """Set ``context`` up for this turn and return its token state, which
    persists across the turns of one context."""
    # Delegation inherits this turn's effective settings, not a fresh env load
    # or a stale config left on a reused context. Do not mutate the caller's cfg.
    context.config = replace(
        cfg, model=request.model, provider_id=request.provider_id,
        provider_base_url=request.base_url, provider_api_key=request.api_key,
        reasoning_effort=request.effort, max_output_tokens=request.max_out,
        vision_enabled=vision_enabled_for_model(request.model, getattr(cfg, "settings", None)),
    )
    context.tool_registry = registry
    context.agent_id = cfg.agent_id
    context.configure_snapshot_store(cfg.agent_id, cfg.session_file)
    _turn_settings.install(context, cfg)
    install_context_window_overrides(cfg)
    context.model = request.model
    context.last_incomplete_reason = None
    context.last_output_tokens = 0
    context.last_max_output_tokens = request.max_out
    context.compacted_during_turn = False
    context.context_tokens = 0
    context.tokens_until_compaction = None
    context.turn_status.reset()
    chars_per_token = compaction.get_float(cfg, "chars_per_token")
    token_state = getattr(context, "context_budget_state", None)
    if not isinstance(token_state, context_budget.TokenState):
        token_state = context_budget.TokenState(chars_per_token=chars_per_token)
    else:
        token_state.chars_per_token = chars_per_token
    context.context_budget_state = token_state
    context.vision_enabled = context.config.vision_enabled
    if event_hooks is not None:
        # Subagents started through this context answer to its tool_call guards.
        context.tool_call_hooks = event_hooks
    return token_state


def _trace_banner(request: ModelRequest, context: ToolContext, registry: ToolRegistry,
                  resolve_window: Callable[[], int | None]) -> None:
    """Print the run line: model, provider, window, output cap, effort, vision, tools."""
    model, provider_id = request.model, request.provider_id
    if provider_id:
        provider_label = provider_id
        base = request.base_url or "provider-default"
    else:
        provider_label = "ai-sdk"
        base = model.split(":")[0] if ":" in model else "ai-gateway"
    # ctx is the number that decides when compaction fires and how much room
    # is left to work in; max_out only bounds one reply. Showing the second
    # without the first invites reading 128000 as the window.
    ctx = compaction.configured_context_window(context.config, resolve_window)
    bits = [f"model={model}",
            f"provider={provider_label}",
            f"base={base}",
            f"ctx={ctx if ctx else 'unknown'}",
            f"max_out={request.max_out if request.max_out is not None else 'provider-default'}"]
    if request.effort:
        bits.append(f"effort={request.effort}")
    bits.append(f"vision={'on' if context.vision_enabled else 'off'}")
    try:
        ntools = len(registry.openai_specs())
    except Exception:  # noqa: BLE001 — registry internals
        ntools = "?"
    bits.append(f"tools={ntools}")
    print(f"{display.CHROME}{msgs.RUN_LINE.text(fields='  '.join(bits))}{C.RESET}", flush=True)


def _note_user_skill(messages: list[dict], registry: ToolRegistry) -> None:
    """Mark a skill the user invoked in the turn's opening message as loaded."""
    opening = _last_user_message_index(messages)
    user_skill = skills.user_invoked_skill(messages[opening].get("content")) if opening is not None else None
    note_skill_loaded = getattr(registry, "note_skill_loaded", None)
    if user_skill and callable(note_skill_loaded):
        note_skill_loaded(user_skill)


def _record_assistant(reply: ModelReply, *, cfg: Config, request: ModelRequest, system: str,
                      messages: list[dict], convo: TurnConvo, registry: ToolRegistry,
                      context: ToolContext, token_state: context_budget.TokenState,
                      telemetry: Telemetry, events: TurnEvents, sink: StreamSink,
                      suppress_output: bool) -> tuple[list[_PendingToolCall], str | None, ToolRegistry]:
    """Append the reply to the history and the convo. Returns the tool calls
    to run, the reply's incomplete reason, and the registry that authorizes
    the calls: the surface this model call saw."""
    result = reply.result
    text = result.text
    pending_calls = [
        _PendingToolCall(id=call.id, name=call.name, arg_chunks=[call.arguments])
        for call in result.tool_calls
    ]
    provider_metadata, incomplete_reason = reply.provider_metadata, reply.incomplete_reason
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
            sink.text(("\n\n" if text else "") + notice)
            sink.close()
        text = f"{text}\n\n{notice}" if text else notice
        pending_calls = []
        assistant_message_override = ai.assistant_message(text)

    # Freeze dispatch authorization to the schemas this model call saw.
    # Discovery may mutate the turn surface while this batch runs, but a
    # newly loaded tool is callable only after its schema is emitted on
    # the next model iteration.
    dispatch_registry = registry.dispatch_registry()
    batch_diagnostic_suffix = ""
    if pending_calls:
        pending_calls, batch_stats = _normalize_tool_call_batch(
            pending_calls,
            dispatch_registry,
            getattr(
                cfg,
                "max_tool_calls_per_message",
                _settings.default_value("limits.max_tool_calls_per_message"),
            ),
        )
        telemetry.event(
            "tool_call_batch_normalized",
            received=batch_stats.received,
            retained=batch_stats.retained,
            duplicate=batch_stats.duplicate,
            invalid=batch_stats.invalid,
            capped=batch_stats.capped,
        )
        if batch_stats.capped:
            diagnostic = (
                "Tool-call batch limit reached: "
                f"retained {batch_stats.retained} distinct calls and rejected "
                f"{batch_stats.capped} beyond limits.max_tool_calls_per_message."
            )
            batch_diagnostic_suffix = ("\n\n" if text else "") + diagnostic
            text += batch_diagnostic_suffix

    record: dict = {"role": "assistant", "content": text}
    if pending_calls:
        record["tool_calls"] = [
            {"id": pc.id, "type": "function",
             "function": {"name": pc.name, "arguments": pc.arguments()}}
            for pc in pending_calls
        ]
    if result.reasoning:
        record["reasoning_content"] = result.reasoning
    if not isinstance(provider_metadata, dict):
        provider_metadata = None
    incomplete_reason = incomplete_reason or model_client.incomplete_reason_from_metadata(provider_metadata)
    if provider_metadata:
        record["provider_metadata"] = provider_metadata
    if incomplete_reason:
        record["incomplete_reason"] = incomplete_reason
    assistant_message = assistant_message_override or result.assistant_message
    if result.tool_calls:
        assistant_message = _assistant_message_with_tool_calls(
            assistant_message,
            pending_calls,
            diagnostic_suffix=batch_diagnostic_suffix,
        )
    # The signed parts as this turn replays them, after batch normalization.
    signed_reasoning = (
        model_client.signed_reasoning_parts(assistant_message)
        if assistant_message_override is None else None
    )
    if signed_reasoning:
        record["reasoning_parts"] = signed_reasoning
        record["reasoning_from"] = reasoning_rules.reasoning_origin(request.provider_id, request.model)
    if provider_metadata and not getattr(assistant_message, "provider_metadata", None):
        assistant_message = assistant_message.model_copy(update={"provider_metadata": provider_metadata})
    convo.ai.append(_sanitize_assistant_message(assistant_message))
    messages.append(memory.note_time(record))
    # Recorded in full now; a later ^C in this turn must not re-append it.
    sink.clear()
    token_state.record_provider_usage(
        None if reply.usage_stale else result.usage,
        message_count=len(messages),
        messages=messages,
        system=system,
        tools=reply.ai_tools,
    )
    current_tokens, _estimate, used_provider = token_state.current_context_tokens(
        system=system,
        messages=messages,
        tools=reply.ai_tools,
    )
    context.context_tokens = current_tokens
    context.context_tokens_used_provider_usage = used_provider
    if text:
        payload = {"text": text, "finish_reason": reply.finish}
        if incomplete_reason:
            payload["incomplete_reason"] = incomplete_reason
        events.emit("response", **payload)
    return pending_calls, incomplete_reason, dispatch_registry


def _send_resume_nudge(messages: list[dict], convo: TurnConvo, *, n: int, limit: int,
                       incomplete_reason: str | None, model: str, telemetry: Telemetry,
                       suppress_output: bool) -> None:
    """Keep the partial reply and ask the model to carry on from where the
    cap cut it."""
    nudge = {"role": "user", "content": MAX_OUTPUT_RESUME_NUDGE, "resume_nudge": True}
    messages.append(nudge)
    convo.add([nudge])
    telemetry.event("max_output_resume", model=model, n=n, incomplete_reason=incomplete_reason)
    if not suppress_output:
        msgs.warn(msgs.MAX_OUTPUT_RESUMING, n=n, limit=limit)


async def _record_tool_results(pending_calls: list[_PendingToolCall], *, cfg: Config, trace: bool,
                               messages: list[dict], convo: TurnConvo, registry: ToolRegistry,
                               dispatch_registry: ToolRegistry, context: ToolContext,
                               error_tracker: ToolErrorTracker, telemetry: Telemetry,
                               events: TurnEvents) -> list[tuple[_PendingToolCall, dict, Any]]:
    """Run the batch and append its results to the history and the convo.
    Returns the (call, arguments, result) records of the calls that ran.

    convo.ai carries the heavy form (image bytes embedded in tool messages)
    for THIS turn; messages, persisted and replayed on every future turn,
    carries the dehydrated stub so base64 is billed once."""
    for index, pc in enumerate(pending_calls):
        emission = events.emit(
            "tool_call",
            id=pc.id,
            name=_canonical_tool_call_name(pc.name, registry),
            arguments=_canonical_tool_args(pc.arguments()),
        )
        refusal = event_mod.refusal_of(emission)
        if refusal and pc.validation_error is None:
            # An `on tool_call` handler refused it: the call never runs
            # and the model reads the refusal as its result.
            pending_calls[index] = replace(pc, validation_error=refusal, refused=True)
            telemetry.event("tool_call_refused", tool=pc.name, refusal=refusal)
    # Tools are sync (subprocess, file I/O); leaf calls fan out to a worker
    # thread so the shared loop stays free while they execute. Fan-out (task /
    # named-agent) calls are awaited ON the loop instead, so a parent turn
    # never parks a dispatch thread its descendants need (see _dispatch_batch).
    # A read that repeats an earlier one returns a stub naming it only
    # while that earlier result is still in the history the model sees.
    context.keep_shown_reads(_tool_results_in(messages))
    progress = _DispatchProgress()
    context.turn_status.tool_begin([_canonical_tool_call_name(pc.name, registry) for pc in pending_calls])
    try:
        dispatch_records = await _dispatch_batch(
            pending_calls,
            telemetry,
            cfg.max_tool_result_bytes,
            trace,
            error_tracker,
            dispatch_registry,
            context,
            asyncio.get_running_loop(),
            progress,
        )
    finally:
        context.turn_status.tool_end()
        # Also runs on cancellation, before the REPL persists the turn
        # and balances genuinely unanswered calls with orphan markers.
        dispatch_records = [progress.records[pc.id] for pc in pending_calls
                            if pc.id in progress.records]
        capped = _cap_batch_results(
            [r for _, _, r in dispatch_records],
            getattr(cfg, "max_tool_results_per_turn_bytes", 0),
        )
        reconciled: list[tuple[_PendingToolCall, dict, Any]] = []
        for (pc, args, old_result), new_result in zip(dispatch_records, capped, strict=True):
            _reconcile_read_delivery(
                _canonical_tool_call_name(pc.name, registry),
                args,
                old_result,
                new_result,
                context,
                pc.id,
            )
            reconciled.append((pc, args, new_result))
        dispatch_records = reconciled
        context.settle_reads()
        # A batch's tool results must stay contiguous: the SDK's history
        # check ends the pending tool-call window at the first following
        # user/assistant message, so an image's user FilePart inserted
        # between two tool results orphans every later one. Collect the
        # batch's tool messages first, then the media that follows it.
        batch_tool_msgs: list[ai.messages.Message] = []
        batch_media_msgs: list[ai.messages.Message] = []
        for pc, _args, result_value in dispatch_records:
            canonical_pc = _pending_with_name(pc, _canonical_tool_call_name(pc.name, registry))
            events.emit(
                "tool_result",
                id=pc.id,
                name=canonical_pc.name,
                result=result_value,
            )
            for built in model_client.build_tool_result_messages(pc.id, pc.name, result_value):
                if built.role == "tool":
                    batch_tool_msgs.append(built)
                else:
                    batch_media_msgs.append(built)
            done_at = progress.finished.get(pc.id)
            messages.extend(memory.note_time(item, done_at)
                            for item in _history_tool_result_message(canonical_pc, result_value))
        convo.ai.extend(batch_tool_msgs)
        convo.ai.extend(batch_media_msgs)
    return dispatch_records


def _record_tool_error_limit(dispatch_records: list[tuple[_PendingToolCall, dict, Any]], *,
                             registry: ToolRegistry, messages: list[dict], convo: TurnConvo,
                             events: TurnEvents) -> None:
    """End the turn on an assistant message naming the last failed call."""
    name, last_error = next(
        ((_canonical_tool_call_name(pc.name, registry), result_value)
         for pc, _, result_value in reversed(dispatch_records)
         if isinstance(result_value, str) and result_value.startswith("ERROR")),
        (dispatch_records[-1][0].name, dispatch_records[-1][2]),
    )
    failure = f"ERROR: tool retry limit reached after {name}\n{last_error}"
    convo.ai.append(ai.messages.Message(role="assistant", parts=[ai.types.messages.TextPart(text=failure)]))
    messages.append(memory.note_time({"role": "assistant", "content": failure}))
    events.emit("error", error=failure, retryable=False)
    events.end("tool_error_limit")


async def _take_steer(steer: Callable[[], dict | None | Awaitable[dict | None]], messages: list[dict],
                      convo: TurnConvo, telemetry: Telemetry, suppress_output: bool) -> None:
    """Append the message ``steer`` returns, if any, at this tool boundary."""
    steered = steer()
    if inspect.isawaitable(steered):
        steered = await steered
    if steered is not None:
        messages.append(memory.note_time(steered))
        convo.add([steered])
        telemetry.event("steered", message_index=len(messages) - 1)
        if not suppress_output:
            msgs.say(msgs.STEERED, flush=True)


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
             event_hooks: event_mod.EventHooks | event_mod.RefusableOnly | None = None,
             mcp_host: Any = None,
             steer: Callable[[], dict | None | Awaitable[dict | None]] | None = None,
             event_sink: Callable[[str, dict], None] | None = None) -> None:
    """One user turn → tool-use loop until the model stops. The real primitive:
    it awaits the model stream and runs tool dispatch in a thread executor, so it
    NEVER blocks the loop — many turns/subagents run concurrently. Mutates
    `messages` in place so the caller can persist new entries.

    ``steer`` is called at each tool boundary: after a batch's results are
    recorded, when another model call follows. It returns a user message or
    None, directly or as an awaitable. A returned message is appended there, so
    the model reads it before choosing its next tool call.

    ``event_sink`` sees every event this turn emits, as ``(event, payload)``:
    the `events.CANONICAL_EVENT_NAMES` the turn raises, with the turn's usage
    totals added to ``turn_end``, and ``usage`` after each model call charged
    to the session (`js.usage`). Subagent turns do not reach it.

    Provider overrides let the REPL /prompt mode switch endpoint without
    reloading config; unset values fall back to the Config values. The sync
    ``run_turn`` below wraps this for callers not yet on the async runtime.
    """
    request = _turn_request(
        cfg, model_override=model_override, provider_id_override=provider_id_override,
        provider_base_url_override=provider_base_url_override,
        provider_api_key_override=provider_api_key_override,
        reasoning_effort_override=reasoning_effort_override, max_output_override=max_output_override,
        sampling=sampling,
    )
    model, provider_id = request.model, request.provider_id
    convo = TurnConvo(system, messages, provider_id=provider_id, model=model)
    base_registry = tool_registry or T.STOCK_REGISTRY
    alias_map = _resolve_alias_profile(getattr(cfg, "settings", {}) or {}, model, provider_id, base_registry)
    active_context = tool_context or T.STOCK_CONTEXT
    owns_mcp_host = mcp_host is None
    if owns_mcp_host and getattr(cfg, "mcp", None) is not None and getattr(cfg.mcp, "servers", ()):
        from .mcp.host import MCPHost

        mcp_host = MCPHost(cfg.mcp, telemetry=telemetry)
    # A fresh registry rechecks current policy and aliases while restoring the
    # session's visibility. Marks survive compaction and process restarts.
    active_registry = base_registry.aliased(alias_map).lazy_surface(active_context.cwd, mcp_host=mcp_host)
    surface = SurfaceJournal(getattr(cfg, "session_file", None), agent_id=cfg.agent_id,
                             cwd=active_context.cwd, registry=active_registry)
    token_state = _prepare_turn_context(active_context, cfg, request, active_registry, event_hooks)
    turn_status = active_context.turn_status

    events = TurnEvents(event_hooks, event_sink, telemetry, model=model, provider_id=provider_id)
    if mcp_host is not None:
        mcp_host.telemetry = telemetry
        mcp_host.event_sink = events.emit
    events.start(len(messages))

    trace = trace_override if trace_override is not None else cfg.trace
    resolve_window = functools.partial(_resolve_context_window, model, provider_id, request.base_url)
    if trace:
        _trace_banner(request, active_context, active_registry, resolve_window)

    sink = StreamSink(telemetry, turn_status, events, settings=getattr(cfg, "settings", None),
                      suppress_output=suppress_output)
    budget = TurnBudget(
        replace(cfg, model=model, provider_id=provider_id,
                provider_base_url=request.base_url, provider_api_key=request.api_key),
        convo, token_state, active_context, telemetry=telemetry, turn_status=turn_status,
        emit=events.emit, resolve_window=resolve_window, max_out=request.max_out,
    )
    limits = CallLimits.from_settings(getattr(cfg, "settings", None))
    caller = ModelCaller(
        request, limits, convo=convo, budget=budget, sink=sink, events=events, telemetry=telemetry,
        context=active_context, turn_status=turn_status, registry=active_registry,
        alias=functools.partial(_aliased_tool_specs, alias_map=alias_map), mcp_host=mcp_host,
        call_stats=call_stats, trace=trace, suppress_output=suppress_output,
    )
    error_tracker = ToolErrorTracker()

    net_role_token = stream_transport.set_role(
        active_context.net_label, agent=cfg.agent_id, status=turn_status, retries=True,
    )
    usage_token = usage_mod.start(usage_mod.Meter(
        (getattr(cfg, "session_file", None), *getattr(active_context, "usage_chain", ())),
        on_call=events.on_usage,
    ))
    try:
        await surface.restore()
        _note_user_skill(messages, active_registry)
        durable_side_effects_started = False
        resumes_sent = 0
        for iteration in range(cfg.max_tool_iterations):
            reply = await caller.call(phase="midturn" if durable_side_effects_started else "preflight")
            if reply is None:
                stream_transport.report_held_failure()
                msgs.say(msgs.RETRY_BUDGET_EXHAUSTED)
                events.end("retry_budget_exhausted")
                return
            pending_calls, incomplete_reason, dispatch_registry = _record_assistant(
                reply, cfg=cfg, request=request, system=system, messages=messages, convo=convo,
                registry=active_registry, context=active_context, token_state=token_state,
                telemetry=telemetry, events=events, sink=sink, suppress_output=suppress_output,
            )
            durable_side_effects_started = True
            resuming = (
                not pending_calls
                and resumes_sent < limits.max_output_resumes
                and iteration + 1 < cfg.max_tool_iterations
                and cut_off_by_cap(incomplete_reason, [])
            )
            if incomplete_reason and not suppress_output and not resuming:
                msgs.warn(msgs.RESPONSE_INCOMPLETE, reason=incomplete_reason)
            if resuming:
                resumes_sent += 1
                _send_resume_nudge(messages, convo, n=resumes_sent, limit=limits.max_output_resumes,
                                   incomplete_reason=incomplete_reason, model=model,
                                   telemetry=telemetry, suppress_output=suppress_output)
                continue
            if not pending_calls:
                if incomplete_reason:
                    events.end("incomplete", finish_reason=reply.finish, incomplete_reason=incomplete_reason)
                else:
                    events.end("stop")
                return
            dispatch_records = await _record_tool_results(
                pending_calls, cfg=cfg, trace=trace, messages=messages, convo=convo,
                registry=active_registry, dispatch_registry=dispatch_registry, context=active_context,
                error_tracker=error_tracker, telemetry=telemetry, events=events,
            )
            if error_tracker.limit_reached():
                _record_tool_error_limit(dispatch_records, registry=active_registry, messages=messages,
                                         convo=convo, events=events)
                return
            if steer is not None and iteration + 1 < cfg.max_tool_iterations:
                await _take_steer(steer, messages, convo, telemetry, suppress_output)

        msgs.say(msgs.MAX_ITERATIONS, limit=cfg.max_tool_iterations)
        events.end("max_iterations")
    except BaseException as _turn_exc:  # noqa: BLE001
        # turn_start is emitted unconditionally and every normal/handled exit
        # already emitted turn_end; only cancellation (CancelledError /
        # KeyboardInterrupt — BaseException, not Exception) reaches here
        # unbalanced, so pair turn_start with a turn_end before propagating.
        if not isinstance(_turn_exc, Exception):
            sink.close()
            sink.commit_partial(messages)
            events.end("cancelled")
        else:
            stream_transport.report_held_failure()
        raise
    finally:
        sink.close_reasoning()
        usage_mod.stop(usage_token)
        stream_transport.reset_role(net_role_token)
        turn_status.reset()
        if owns_mcp_host and mcp_host is not None:
            await mcp_host.close()


def run_turn(*args, loop_runner: asyncio.Runner | None = None, **kwargs) -> None:
    """Sync wrapper over :func:`run_turn_async` — spins a throwaway loop for this
    turn. The OLD blocking path; the non-blocking runtime awaits
    ``run_turn_async`` directly on its shared loop. Kept so the current sync
    callers (cli one-shot/REPL/bench, subagent threads) keep working through the
    transition. `messages` is still mutated in place, so ^C mid-turn preserves
    partial work exactly as before.
    """
    if loop_runner is not None:
        return loop_runner.run(run_turn_async(*args, **kwargs))
    return model_client.run_owning_loop(run_turn_async(*args, **kwargs))
