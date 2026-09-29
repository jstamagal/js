"""Tool-use loop. Streaming output, typed error handling, telemetry.
Uses ``js.model_client`` for model I/O via the Vercel AI Python SDK (``ai``)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Awaitable, Callable
import asyncio
import contextlib
import inspect
import json
import hashlib
import itertools
import os
import sys
import threading
from pathlib import Path
import random
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from . import events as event_mod
from . import model_client, memory
import ai

from . import colors as C
from . import context_budget
from . import display
from . import messages as msgs
from .text_bytes import byte_size, byte_prefix, cap_text
from . import model_metadata
from . import paths
from . import providers
from . import settings as _settings
from . import toolkit as T
from . import tool_args
from . import routing
from . import compaction
from . import stream_transport
from .config import Config, vision_enabled_for_model
from .sampling import Sampling
from .reasoning_display import ReasoningDisplay, StderrReasoning
from .toolkit.core import (ToolContext, ToolResult, call_is_read_only, call_scope, call_tool,
                           call_tool_async, registry_scope)
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


def _backoff(attempt: int) -> float:
    """Exponential with jitter: 1s, 2s, 4s ... capped."""
    base = min(2 ** attempt, 16)
    return base + random.uniform(0, 1)


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
    """Replace raw SDK tool-call parts with the normalized batch."""
    original = {
        part.tool_call_id: part
        for part in message.parts
        if isinstance(part, ai.types.messages.ToolCallPart)
    }
    parts = [
        part
        for part in message.parts
        if not isinstance(part, ai.types.messages.ToolCallPart)
    ]
    if diagnostic_suffix:
        parts.append(ai.types.messages.TextPart(text=diagnostic_suffix))
    for call in calls:
        prior = original.get(call.id)
        if prior is None:
            part = ai.types.messages.ToolCallPart(
                tool_call_id=call.id,
                tool_name=call.name,
                tool_args=call.arguments(),
            )
        else:
            part = prior.model_copy(
                update={"tool_name": call.name, "tool_args": call.arguments()}
            )
        parts.append(part)
    if not parts:
        parts.append(ai.types.messages.TextPart(text=""))
    return message.model_copy(update={"parts": parts})


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
        with call_scope(call_id), registry_scope(active_registry):
            result = call_tool(tool, args, context)
        telemetry.event("tool_ok", tool=tool.name, latency_ms=int((time.time() - started) * 1000))
    except Exception as e:  # noqa: BLE001
        telemetry.event("tool_exception", tool=tool.name,
                        error=f"{type(e).__name__}: {e}",
                        latency_ms=int((time.time() - started) * 1000))
        result = f"ERROR running {tool.name}: {type(e).__name__}: {e}"
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

    def record(self, pc: _PendingToolCall, args: dict, result: Any) -> None:
        self.records[pc.id] = (pc, args, result)


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
        result = f"ERROR running {tool.name}: {type(e).__name__}: {e}"
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
        with call_scope(pc.id), registry_scope(registry):
            result = await call_tool_async(tool, args, tool_context)
        telemetry.event("tool_ok", tool=tool.name, latency_ms=int((time.time() - started) * 1000))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        telemetry.event("tool_exception", tool=tool.name, error=f"{type(exc).__name__}: {exc}")
        result = f"ERROR running {tool.name}: {type(exc).__name__}: {exc}"
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

def _last_user_message_index(messages: list[dict]) -> int | None:
    """Index of the message that opened the current turn. A steered message
    joined a turn already running, so it does not open one."""
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user" and not messages[idx].get("steered"):
            return idx
    return None


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
             event_hooks: event_mod.EventHooks | None = None,
             mcp_host: Any = None,
             steer: Callable[[], dict | None | Awaitable[dict | None]] | None = None) -> None:
    """One user turn → tool-use loop until the model stops. The real primitive:
    it awaits the model stream and runs tool dispatch in a thread executor, so it
    NEVER blocks the loop — many turns/subagents run concurrently. Mutates
    `messages` in place so the caller can persist new entries.

    ``steer`` is called at each tool boundary: after a batch's results are
    recorded, when another model call follows. It returns a user message or
    None, directly or as an awaitable. A returned message is appended there, so
    the model reads it before choosing its next tool call.

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
        max_out = model_metadata.resolve_max_output(model, provider_id)
    ai_convo = model_client.history_to_ai_messages(system, messages, provider_id=provider_id)
    error_tracker = ToolErrorTracker()
    base_registry = tool_registry or T.STOCK_REGISTRY
    alias_map = _resolve_alias_profile(getattr(cfg, "settings", {}) or {}, model, provider_id, base_registry)
    active_context = tool_context or T.STOCK_CONTEXT
    # Delegation inherits this turn's effective settings, not a fresh env load
    # or a stale config left on a reused context. Do not mutate the caller's cfg.
    active_context.config = replace(
        cfg, model=model, provider_id=provider_id,
        provider_base_url=provider_base_url, provider_api_key=provider_api_key,
        reasoning_effort=effort, max_output_tokens=max_out,
        vision_enabled=vision_enabled_for_model(model, getattr(cfg, "settings", None)),
    )
    owns_mcp_host = mcp_host is None
    if owns_mcp_host and getattr(cfg, "mcp", None) is not None and getattr(cfg.mcp, "servers", ()):
        from .mcp.host import MCPHost

        mcp_host = MCPHost(cfg.mcp, telemetry=telemetry)
    # A fresh registry rechecks current policy and aliases while restoring the
    # session's visibility. Marks survive compaction and process restarts.
    active_registry = base_registry.aliased(alias_map).lazy_surface(active_context.cwd, mcp_host=mcp_host)
    surface_file = getattr(cfg, "session_file", None)
    if surface_file is not None and Path(surface_file).resolve() == Path(os.devnull):
        surface_file = None
    surface_scope = {"version": 1, "agent_id": cfg.agent_id, "cwd": str(active_context.cwd.resolve())}
    prior_surface = memory.load_tool_surface(surface_file) if surface_file is not None else None
    last_surface = active_registry.snapshot()

    def save_surface(state: dict) -> None:
        nonlocal last_surface
        if state != last_surface:
            if surface_file is not None:
                memory.append_tool_surface(surface_file, {**surface_scope, **state})
            last_surface = state

    active_context.tool_registry = active_registry
    active_context.agent_id = cfg.agent_id
    active_context.configure_snapshot_store(cfg.agent_id, cfg.session_file)
    # One conversation, one cache key: OpenAI-compatible endpoints use it to route
    # a request to the machine already holding that conversation's prefix. Keyed on
    # the session rather than the agent so two concurrent sessions do not contend
    # for one prefix. A session-less run sends no key and falls back to the
    # provider's own longest-prefix matching.
    _session_file = getattr(cfg, "session_file", None)
    _cache_key = (
        f"js-{cfg.agent_id}-{Path(_session_file).stem}"
        if _session_file is not None and Path(_session_file).name not in ("", os.devnull, "null")
        else None
    )
    active_context.max_tool_result_bytes = getattr(cfg, "max_tool_result_bytes", active_context.max_tool_result_bytes)
    active_context.max_bash_output_bytes = getattr(cfg, "max_bash_output_bytes", active_context.max_bash_output_bytes)
    active_context.fetch_timeout_s = getattr(cfg, "fetch_timeout_s", active_context.fetch_timeout_s)
    active_context.shell_env_allow = getattr(cfg, "shell_env_allow", active_context.shell_env_allow)
    active_context.browse_timeout_s = getattr(cfg, "browse_timeout_s", active_context.browse_timeout_s)
    active_context.download_timeout_s = getattr(cfg, "download_timeout_s", active_context.download_timeout_s)
    active_context.max_download_bytes = getattr(cfg, "max_download_bytes", active_context.max_download_bytes)
    active_context.max_read_lines = getattr(cfg, "max_read_lines", active_context.max_read_lines)
    active_context.max_read_bytes = getattr(cfg, "max_read_bytes", active_context.max_read_bytes)
    active_context.max_tool_result_inline_bytes = getattr(cfg, "max_tool_result_inline_bytes", active_context.max_tool_result_inline_bytes)
    active_context.max_bash_output_ceiling = getattr(cfg, "max_bash_output_ceiling", active_context.max_bash_output_ceiling)
    install_context_window_overrides(cfg)
    active_context.max_file_bytes = getattr(cfg, "max_file_bytes", active_context.max_file_bytes)
    active_context.model = model
    active_context.kernel_verbosity = getattr(cfg, "kernel_verbosity", active_context.kernel_verbosity)
    active_context.kernel_render_max_lines = getattr(cfg, "kernel_render_max_lines", active_context.kernel_render_max_lines)
    active_context.kernel_wait_seconds = getattr(cfg, "kernel_wait_seconds", active_context.kernel_wait_seconds)
    active_context.shell_wait_seconds = getattr(cfg, "shell_wait_seconds", active_context.shell_wait_seconds)
    active_context.max_parallel_tools = getattr(cfg, "max_parallel_tools", active_context.max_parallel_tools)
    active_context.task_max_depth = getattr(cfg, "task_max_depth", active_context.task_max_depth)
    active_context.subagent_max_workers = getattr(cfg, "subagent_max_workers", active_context.subagent_max_workers)
    live_settings = getattr(cfg, "settings", None)
    active_context.user_agent = _settings.knob(live_settings, "tools.user_agent")
    active_context.shell_program = _settings.knob(live_settings, "shell.program")
    active_context.jail_bind = tuple(_settings.knob(live_settings, "jail.bind") or ())
    active_context.terminal_cols = _settings.knob(live_settings, "tools.terminal_cols")
    active_context.terminal_rows = _settings.knob(live_settings, "tools.terminal_rows")
    active_context.lsp_servers = _settings.knob(live_settings, "lsp.servers")
    active_context.lsp_timeout_s = _settings.knob(live_settings, "lsp.timeout_s")
    active_context.notebook_output_lines = _settings.knob(live_settings, "notebook.output_lines")
    active_context.last_incomplete_reason = None
    active_context.last_output_tokens = 0
    active_context.last_max_output_tokens = max_out
    active_context.compacted_during_turn = False
    active_context.context_tokens = 0
    active_context.tokens_until_compaction = None
    turn_status = active_context.turn_status
    turn_status.reset()
    chars_per_token = compaction.get_float(cfg, "chars_per_token")
    token_state = getattr(active_context, "context_budget_state", None)
    if not isinstance(token_state, context_budget.TokenState):
        token_state = context_budget.TokenState(chars_per_token=chars_per_token)
    else:
        token_state.chars_per_token = chars_per_token
    active_context.context_budget_state = token_state
    active_context.vision_enabled = active_context.config.vision_enabled

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

    if mcp_host is not None:
        mcp_host.telemetry = telemetry
        mcp_host.event_sink = lambda event, **payload: _emit_event(event, **payload)

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
        _ctx_for_banner = compaction.configured_context_window(
            active_context.config,
            lambda: _resolve_context_window(model, provider_id, provider_base_url),
        )
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
        print(f"{display.CHROME}{msgs.RUN_LINE.text(fields='  '.join(_bits))}{C.RESET}", flush=True)

    # One Display per streamed answer, opened at its first chunk and finished
    # when the stream ends.
    answer_display: display.Display | None = None
    markdown = display.markdown_enabled(getattr(cfg, "settings", None))
    # Text already displayed but not yet recorded. The assistant record is only
    # built after the stream completes, so a ^C mid-stream would otherwise leave
    # the answer on screen and nothing in history.
    streamed_text = {"value": ""}
    _transcript_log = getattr(telemetry, "transcript_log", None)
    streamed_reasoning: list[str] = []
    reasoning_display: ReasoningDisplay | None = None
    reasoning_level = _settings.knob(getattr(cfg, "settings", None), "ui.reasoning")
    if not isinstance(reasoning_level, int) or reasoning_level not in range(4):
        reasoning_level = _settings.default_value("ui.reasoning")

    def _emit_reasoning(chunk: str) -> None:
        nonlocal reasoning_display
        if not chunk:
            return
        streamed_reasoning.append(chunk)
        turn_status.stream(chunk)
        if suppress_output or reasoning_level == 0:
            return
        if reasoning_display is None:
            factory = telemetry.reasoning_factory
            reasoning_display = (
                factory(reasoning_level) if factory is not None
                else StderrReasoning(reasoning_level, sys.stderr)
            )
        reasoning_display.append(chunk)

    def _close_reasoning(tokens: int | None = None) -> None:
        nonlocal reasoning_display
        if reasoning_display is not None:
            reasoning_display.finish(tokens)
            reasoning_display = None

    def _muted_transcript_tee():
        mute = getattr(_transcript_log, "mute_tee", None)
        if callable(mute):
            return mute()
        return contextlib.nullcontext()

    def _emit_text(t: str) -> None:
        nonlocal answer_display
        if not t:
            return
        if reasoning_display is not None:
            reasoning_display.answer_started()
        streamed_text["value"] += t
        turn_status.stream(t)
        _emit_event("stream", text=t)
        if suppress_output:
            return
        if _transcript_log is not None:
            write_chunk = getattr(_transcript_log, "write_assistant_chunk", None)
            if callable(write_chunk):
                write_chunk(t)
        with _muted_transcript_tee():
            if answer_display is None:
                factory = telemetry.display_factory
                answer_display = (
                    factory(markdown) if factory is not None
                    else display.Display.for_stream(sys.stdout, markdown=markdown)
                )
            answer_display.chunk("text", t)

    def _commit_streamed_partial() -> None:
        """Record received text and reasoning before cancellation.

        A partial assistant record marks progress even when its reasoning was
        hidden, so the caller preserves the turn rather than discarding it.
        """
        partial = streamed_text["value"]
        partial_reasoning = "".join(streamed_reasoning)
        streamed_text["value"] = ""
        streamed_reasoning.clear()
        if not partial and not partial_reasoning:
            return
        record = {"role": "assistant", "content": partial, "incomplete_reason": "cancelled"}
        if partial_reasoning:
            record["reasoning_content"] = partial_reasoning
        messages.append(record)

    def _close_text(reasoning_tokens: int | None = None) -> None:
        nonlocal answer_display
        if not suppress_output and answer_display is not None:
            if _transcript_log is not None:
                end_stream = getattr(_transcript_log, "end_assistant_stream", None)
                if callable(end_stream):
                    end_stream()
            with _muted_transcript_tee():
                answer_display.finish()
        answer_display = None
        _close_reasoning(reasoning_tokens)

    # Full request trace: dump system prompt + full tool schemas once (first
    # model call), then only the newly-sent messages each call. This goes ONLY to
    # the trace sink (autolog file / --debug-file), never to stdout — decoupled
    # from the concise `trace` flag that drives the run/stats/tool lines on the terminal.
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
        return compaction.configured_context_window(
            active_compact_cfg,
            lambda: _resolve_context_window(model, provider_id, provider_base_url),
        )

    def _budget_buffer_tokens() -> int:
        return compaction.get_nonnegative_int(active_compact_cfg, "buffer_tokens")

    def _active_preserve_from() -> int | None:
        return _last_user_message_index(messages)

    async def _maybe_compact_request_for_budget(
        *,
        phase: str,
        specs: list[dict],
        force: bool = False,
    ) -> bool:
        """Bring the next request under budget. Escalates in three steps, each
        costlier than the last and each stopping as soon as the budget is met:
        clear old tool-result bodies, summarize the history before the current
        user message, then summarize the current turn itself keeping only its
        tail. Returns True when the history changed."""
        nonlocal ai_convo
        if not force and not compaction.get_bool(active_compact_cfg, "auto"):
            return False
        context_window = _budget_context_window()
        if context_window <= 0 and not force:
            return False
        ai_tools_for_budget = model_client.tool_specs_to_ai_tools(specs) if specs else None
        reserved = context_window - compaction.effective_context_window(active_compact_cfg, context_window)
        buffer_tokens = min(_budget_buffer_tokens(), max(0, reserved))
        status = token_state.budget_status(
            system=system,
            messages=messages,
            tools=ai_tools_for_budget,
            context_window=context_window if context_window > 0 else None,
            output_reserve_tokens=max(0, reserved - buffer_tokens),
            buffer_tokens=buffer_tokens,
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
        trigger = {"phase": phase, "context_tokens": status.current_context_tokens,
                   "context_window": context_window,
                   "effective_input_limit": status.effective_input_limit,
                   "forced_recovery": force}
        flight_data = {"budget": asdict(status), "tools": specs,
                       "usage_anchor": vars(token_state).get("_anchor"),
                       "ai_messages": ai_convo}
        chars_per_token = token_state.calibrated_chars_per_token(
            system=system, messages=messages, tools=ai_tools_for_budget,
        )
        reclaimed = 0
        changed = False

        def _over_budget(reclaimed_chars: int) -> bool:
            # The provider-anchored count minus what was removed, in the
            # currency the anchor was calibrated in.
            if status.effective_input_limit is None:
                return False
            remaining = status.current_context_tokens - int(reclaimed_chars / chars_per_token)
            return remaining > status.effective_input_limit

        def _history_changed() -> None:
            nonlocal changed, ai_convo
            changed = True
            token_state.reset()
            ai_convo = model_client.history_to_ai_messages(system, messages, provider_id=provider_id)
            _trace_req["sent"] = 0
            _trace_req["schemas"] = True
            active_context.compacted_during_turn = True

        # 1. Old tool-result bodies are the bulk of a long turn and cost no
        #    model call to drop.
        cleared, reclaimed = compaction.clear_for_budget(
            messages, cfg=active_compact_cfg, system=system, trigger=trigger,
            flight_data=flight_data, over_budget=_over_budget,
        )
        if cleared:
            _history_changed()
            telemetry.event("context_results_cleared", phase=phase, cleared=cleared)
            if not (force or _over_budget(reclaimed)):
                return True

        async def _summarize(preserve_from: int | None, focus: str, *, tail_tokens: int | None = None) -> bool:
            nonlocal reclaimed
            before_chars = compaction.history_chars(messages)
            turn_status.compacting = True
            try:
                with stream_transport.net_role("Compacting"):
                    result = await compaction.compact_now(
                        active_compact_cfg, system, messages, focus=focus, forced=True,
                        preserve_from=preserve_from, trigger=trigger, flight_data=flight_data,
                        tail_tokens=tail_tokens, context=active_context,
                    )
            except Exception as exc:  # noqa: BLE001
                msgs.warn(msgs.COMPACTION_FAILED, error=f"{type(exc).__name__}: {exc}")
                telemetry.event("context_compaction_failed", phase=phase,
                                error=f"{type(exc).__name__}: {exc}")
                return False
            finally:
                turn_status.compacting = False
            if not compaction.compacted(result):
                telemetry.event("context_compaction_skipped", phase=phase, reason=result)
                return False
            reclaimed += before_chars - compaction.history_chars(messages)
            _history_changed()
            telemetry.event("context_compacted", phase=phase, result=result)
            return True

        # 2. Summarize everything before the current user message, which stays
        #    verbatim along with the turn's work so far.
        preserve_from = _active_preserve_from()
        if (preserve_from is not None and preserve_from > 0
                and compaction.prefix_worth_summarizing(messages, preserve_from)
                and await _summarize(preserve_from, f"{phase} context budget")
                and (force or not _over_budget(reclaimed))):
            return True
        # 3. The current turn alone is over budget: summarize it too, keeping
        #    its most recent tail so the model can carry on from the summary.
        #    A provider rejection (force) says the request did not fit no matter
        #    what the budget believed, so keep half as much tail each round.
        tail_tokens = compaction.get_int(active_compact_cfg, "tail_tokens")
        if force:
            history_tokens = int(compaction.history_chars(messages) / chars_per_token)
            tail_tokens = min(tail_tokens, history_tokens) // 2 ** overflow_recovered
        keep_from = compaction.tail_start(messages, tail_tokens, chars_per_token)
        if keep_from > 0 and compaction.prefix_worth_summarizing(messages, keep_from):
            await _summarize(None, f"{phase} context budget: current turn over budget",
                             tail_tokens=tail_tokens)
        elif not changed:
            telemetry.event("context_compaction_skipped", phase=phase, reason="tail_fills_budget")
        return changed

    net_role_token = stream_transport.set_role(
        active_context.net_label, agent=cfg.agent_id, status=turn_status, retries=True,
    )
    try:
        if prior_surface is not None and all(prior_surface.get(k) == v for k, v in surface_scope.items()):
            await active_registry.restore(prior_surface)
        last_surface = active_registry.snapshot()
        active_registry.on_change = save_surface
        durable_side_effects_started = False
        overflow_recovered = 0
        for iteration in range(cfg.max_tool_iterations):
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
            transport_retries = 0
            for attempt in range(3 + compaction.MAX_OVERFLOW_ROUNDS):
                t0 = time.time()
                try:
                    if mcp_host is not None:
                        await mcp_host.before_model_call()
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
                        tool_names=[spec["function"]["name"] for spec in specs],
                    )
                    streamed_reasoning.clear()
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
                        on_reasoning=_emit_reasoning,
                        provider_headers=getattr(cfg, "provider_headers", None),
                        provider_extra=routing.provider_extra_params(cfg),
                        sampling=sampling,
                        trace_request=_trace_sink is not None,
                        trace_sink=_trace_sink,
                        trace_request_schemas=_trace_req["schemas"],
                        trace_request_from=_trace_req["sent"],
                        cache_key=_cache_key,
                    )
                    if _trace_sink is not None:
                        _trace_req["sent"] = len(ai_convo)
                        _trace_req["schemas"] = False
                    # Await the native async primitive; tolerate a sync override (a
                    # test stub patched onto stream_model_async that returns a result
                    # directly) so the seam accepts either shape.
                    result = await _res if inspect.isawaitable(_res) else _res
                    _close_text(getattr(result.usage, "reasoning_tokens", None))
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
                    turn_status.settle(_out_tok)
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
                    _net = stream_transport.net_level()
                    _label = active_context.net_label
                    if (_net >= 3 and (_label or not suppress_output)) if _net is not None else trace:
                        _elapsed = time.time() - t0
                        _tps = (_out_tok / _elapsed) if _elapsed > 0 else 0.0
                        _cache = ""
                        if active_context.last_prompt_tokens > 0 and active_context.last_cached_tokens > 0:
                            _pct = 100.0 * active_context.last_cached_tokens / active_context.last_prompt_tokens
                            _cache = f"  cache {_pct:.0f}%"
                        _ttft = f"  ttft {int(result.first_token_s * 1000)}ms" if result.first_token_s is not None else ""
                        _stats = msgs.CALL_STATS.text(
                            ms=int(_elapsed * 1000), finish=finish, tool_calls=len(pending_calls),
                            tokens=_out_tok, tps=_tps, ttft=_ttft, cache=_cache)
                        print(f"{display.CHROME}{_label + ': ' if _label else ''}{_stats}{C.RESET}", flush=True)
                    break
                except ai.ProviderAPIError as e:
                    # Finish any partially streamed text before we retry or abort,
                    # so the next attempt's output starts on its own line.
                    _close_text()
                    if (
                        compaction.is_context_overflow_error(e)
                        and overflow_recovered < compaction.MAX_OVERFLOW_ROUNDS
                    ):
                        overflow_recovered += 1
                        telemetry.event(
                            "context_overflow_error",
                            model=model,
                            error=f"{type(e).__name__}: {e}",
                            attempt=attempt,
                            round=overflow_recovered,
                        )
                        # Overflow recovery records old tool-result clearing before retrying.
                        action, cleared, reclaimed = compaction.recover_overflow(
                            messages, overflow_recovered, cfg=active_compact_cfg,
                            system=system, error=e,
                            flight_data={"context_window": _budget_context_window(),
                                         "max_output_tokens": max_out,
                                         "usage_anchor": vars(token_state).get("_anchor"),
                                         "tools": active_registry.openai_specs(),
                                         "ai_messages": ai_convo},
                        )
                        if action == "cleared":
                            token_state.reset()
                            ai_convo = model_client.history_to_ai_messages(system, messages, provider_id=provider_id)
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
                        if transport_retries == 2:
                            _emit_event("error", error=f"{type(e).__name__}: {e}", retryable=True)
                            _end_turn("error")
                            raise
                        stream_transport.say_for_caller(3, f"Retry {transport_retries + 1}: "
                                                           f"{stream_transport.describe_failure(e)}")
                        await asyncio.sleep(_backoff(transport_retries))
                        transport_retries += 1
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
                stream_transport.report_held_failure()
                msgs.say(msgs.RETRY_BUDGET_EXHAUSTED)
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

            # --- Record the assistant turn ---
            history_assistant_record: dict = {"role": "assistant", "content": text}
            if pending_calls:
                history_assistant_record["tool_calls"] = [
                    {"id": pc.id, "type": "function",
                     "function": {"name": pc.name, "arguments": pc.arguments()}}
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
            if result.tool_calls:
                assistant_message = _assistant_message_with_tool_calls(
                    assistant_message,
                    pending_calls,
                    diagnostic_suffix=batch_diagnostic_suffix,
                )
            if provider_metadata and not getattr(assistant_message, "provider_metadata", None):
                assistant_message = assistant_message.model_copy(update={"provider_metadata": provider_metadata})
            ai_convo.append(_sanitize_assistant_message(assistant_message))
            messages.append(history_assistant_record)
            # Recorded in full now; a later ^C in this turn must not re-append it.
            streamed_text["value"] = ""
            streamed_reasoning.clear()
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
                msgs.warn(msgs.RESPONSE_INCOMPLETE, reason=incomplete_reason)

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
            progress = _DispatchProgress()
            turn_status.tool_begin([_canonical_tool_call_name(pc.name, active_registry) for pc in pending_calls])
            try:
                dispatch_records = await _dispatch_batch(
                    pending_calls,
                    telemetry,
                    cfg.max_tool_result_bytes,
                    trace,
                    error_tracker,
                    dispatch_registry,
                    active_context,
                    asyncio.get_running_loop(),
                    progress,
                )
            finally:
                turn_status.tool_end()
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
                        _canonical_tool_call_name(pc.name, active_registry),
                        args,
                        old_result,
                        new_result,
                        active_context,
                        pc.id,
                    )
                    reconciled.append((pc, args, new_result))
                dispatch_records = reconciled
                active_context.settle_reads()
                # A batch's tool results must stay contiguous: the SDK's history
                # check ends the pending tool-call window at the first following
                # user/assistant message, so an image's user FilePart inserted
                # between two tool results orphans every later one. Collect the
                # batch's tool messages first, then the media that follows it.
                batch_tool_msgs: list[ai.messages.Message] = []
                batch_media_msgs: list[ai.messages.Message] = []
                for pc, _args, result_value in dispatch_records:
                    canonical_pc = _pending_with_name(pc, _canonical_tool_call_name(pc.name, active_registry))
                    _emit_event(
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
                    messages.extend(_history_tool_result_message(canonical_pc, result_value))
                ai_convo.extend(batch_tool_msgs)
                ai_convo.extend(batch_media_msgs)
            if error_tracker.limit_reached():
                name, last_error = next(
                    ((_canonical_tool_call_name(pc.name, active_registry), result_value)
                     for pc, _, result_value in reversed(dispatch_records)
                     if isinstance(result_value, str) and result_value.startswith("ERROR")),
                    (dispatch_records[-1][0].name, dispatch_records[-1][2]),
                )
                failure = f"ERROR: tool retry limit reached after {name}\n{last_error}"
                final_error = {"role": "assistant", "content": failure}
                ai_convo.append(ai.messages.Message(role="assistant", parts=[ai.types.messages.TextPart(text=failure)]))
                messages.append(final_error)
                _emit_event("error", error=failure, retryable=False)
                _end_turn("tool_error_limit")
                return
            if steer is not None and iteration + 1 < cfg.max_tool_iterations:
                steered = steer()
                if inspect.isawaitable(steered):
                    steered = await steered
                if steered is not None:
                    messages.append(steered)
                    ai_convo.extend(model_client.history_to_ai_messages("", [steered], provider_id=provider_id))
                    telemetry.event("steered", message_index=len(messages) - 1)
                    if not suppress_output:
                        msgs.say(msgs.STEERED, flush=True)

        msgs.say(msgs.MAX_ITERATIONS, limit=cfg.max_tool_iterations)
        _end_turn("max_iterations")
    except BaseException as _turn_exc:  # noqa: BLE001
        # turn_start is emitted unconditionally and every normal/handled exit
        # already emitted turn_end; only cancellation (CancelledError /
        # KeyboardInterrupt — BaseException, not Exception) reaches here
        # unbalanced, so pair turn_start with a turn_end before propagating.
        if not isinstance(_turn_exc, Exception):
            _close_text()
            _commit_streamed_partial()
            _end_turn("cancelled")
        else:
            stream_transport.report_held_failure()
        raise
    finally:
        _close_reasoning()
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
