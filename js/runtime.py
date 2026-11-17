"""Tool-use loop. Streaming output, typed error handling, telemetry.
Uses ``js.model_client`` for model I/O via the Vercel AI Python SDK (``ai``)."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import socket
import sys  # noqa: F401 - tests patch sys.stdout through runtime.sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import ai

from . import (
    compaction,
    console,
    context_budget,
    context_window,
    events as event_mod,
    held,
    history_record,
    login_defaults,
    memory,
    messages as msgs,
    model_client,
    model_metadata,
    output,
    result_caps,
    retry,
    routing,
    settings as _settings,
    skills,
    snippets,
    stream_transport,
    tool_calls,
    tool_errors,
    tool_result_messages,
    toolkit as T,
    turn_dispatch,
    turn_settings as _turn_settings,
    usage as usage_mod,
)
from .config import Config, vision_enabled_for_model

# The console's printers, reached through runtime by tests of what they print.
from .console import _print_run_line, _print_trace  # noqa: F401
from .reasoning_display import ReasoningDisplay
from .sampling import Sampling
from .toolkit.core import (
    Output,
    ToolContext,
)
from .toolkit.registry import ToolRegistry
from .turn_budget import TurnBudget, TurnConvo, last_user_message_index as _last_user_message_index
from .turn_call import (
    CallLimits,
    ModelCaller,
    ModelReply,
    ModelRequest,
    cut_off_by_cap,
)
from .turn_stream import StreamSink, TurnEvents
from .turn_surface import SurfaceJournal

_UNSET = object()


# --------------------------------------------------------------------------
# Max-output recovery
# --------------------------------------------------------------------------



# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------

@dataclass
class Telemetry:
    debug_log: object  # Path | None — typed loosely to avoid import cycles
    trace_sink: object = None  # a .write()-able sink for the full request trace, or None
    transcript_log: object = None  # visible transcript sink; never raises
    # Where the turn's output events go (`output.Sink`); None means js's own:
    # the transcript record, then the console, which the three factories
    # below steer.
    sink: Any = None
    # Builds the reasoning display for a screen that owns the terminal; None
    # means reasoning is written to sys.stderr.
    reasoning_factory: Callable[[int], ReasoningDisplay] | None = None
    # Builds the answer Display for a screen that owns the terminal; None means
    # a Display over sys.stdout.
    display_factory: Callable[[bool], Any] | None = None
    # Builds the holder of one tool exchange on a screen that can redraw what
    # it has shown: `open(text)` when the call starts, `close(text)` when it
    # ends. None means the exchange is written to sys.stdout as it happens.
    exchange_factory: Callable[[], Any] | None = None
    # js's own sink for this telemetry, made on its first event (`output.sink_of`).
    own_sink: Any = field(default=None, init=False, repr=False)
    event_observer: Callable[[str, dict], None] | None = None

    def event(self, kind: str, **fields: Any) -> None:
        if self.event_observer is not None:
            self.event_observer(kind, fields)
        rec = {"ts": time.time(), "kind": kind, **fields}
        line = json.dumps(rec, default=str) + "\n"
        with output.LOCK:
            if self.trace_sink is not None:
                try:
                    self.trace_sink.write("FLIGHT " + line)
                except OSError as exc:
                    _notice(self, msgs.FLIGHT_LOG_FAILED, stderr=True, flush=True, error=exc)
            if not self.debug_log:
                return
            try:
                with open(self.debug_log, "a") as f:
                    f.write(line)
            except OSError:
                pass  # telemetry must never break the loop


# --------------------------------------------------------------------------
# Tool trace
# --------------------------------------------------------------------------

def _notice(telemetry: Telemetry, message: msgs.Message, *, stderr: bool = False, flush: bool = False,
            **fields: Any) -> None:
    """``message`` as a notice event: a line for stdout, or for stderr."""
    console.emit_event(telemetry, "notice", message=message, fields=fields, stderr=stderr, flush=flush)


# --------------------------------------------------------------------------
# Tool dispatch
# --------------------------------------------------------------------------

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
    return ModelRequest(
        model=model,
        provider_id=provider_id,
        base_url=provider_base_url_override if provider_base_url_override is not None else cfg.provider_base_url,
        api_key=provider_api_key_override if provider_api_key_override is not None else cfg.provider_api_key,
        effort=cfg.reasoning_effort if reasoning_effort_override is _UNSET else reasoning_effort_override,
        max_out=max_out,
        thinking_budget=getattr(cfg, "thinking_budget", None),
        headers=model_client.turn_headers(getattr(cfg, "provider_headers", None), cfg),
        extra=routing.provider_extra_params(cfg),
        sampling=sampling,
        cache_key=_cache_key(cfg),
        settings=getattr(cfg, "settings", None),
    )


def _with_login_defaults(cfg: Config, model_override: str | None, provider_id_override: str | None,
                         provider_base_url_override: str | None) -> Config:
    """``cfg`` with the saved login defaults of the model this turn runs."""
    turn = _overridden(cfg, model_override, provider_id_override, provider_base_url_override)
    return replace(login_defaults.for_turn(turn), model=cfg.model, provider_id=cfg.provider_id,
                   provider_base_url=cfg.provider_base_url)


def _overridden(cfg: Config, model_override: str | None, provider_id_override: str | None,
                provider_base_url_override: str | None) -> Config:
    """``cfg`` routed to the model, provider and base URL this turn runs."""
    return replace(
        cfg,
        model=model_override or cfg.model,
        provider_id=provider_id_override if provider_id_override is not None else cfg.provider_id,
        provider_base_url=(provider_base_url_override if provider_base_url_override is not None
                           else cfg.provider_base_url),
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
                          registry: ToolRegistry, event_hooks: Any,
                          approve: event_mod.Approve | event_mod.Approval | None = None,
                          *, state_dir: Path | None = None) -> context_budget.TokenState:
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
    context.configure_snapshot_store(cfg.agent_id, cfg.session_file, state_dir=state_dir)
    _turn_settings.install(context, cfg)
    context_window.install_context_window_overrides(cfg)
    context.model = request.model
    context.last_incomplete_reason = None
    context.last_output_tokens = 0
    context.last_max_output_tokens = request.max_out
    context.compacted_during_turn = False
    context.context_tokens = 0
    context.tokens_until_compaction = None
    context.turn_status.reset()
    chars_per_token = compaction.get_float(cfg, "COMPACTION_CHARS_PER_TOKEN")
    token_state = context.context_budget_state
    if token_state is None:
        token_state = context_budget.TokenState(chars_per_token=chars_per_token)
    else:
        token_state.chars_per_token = chars_per_token
    context.context_budget_state = token_state
    context.vision_enabled = context.config.vision_enabled
    # Subagents started through this context answer to this turn's tool_call guards.
    context.tool_call_hooks = event_hooks
    # Each turn owns its cancellation scope; child decisions also pass through
    # the parent's approval on the parent's loop.
    if isinstance(approve, event_mod.Approval):
        context.tool_approval = event_mod.Approval(approve.refusal, approve.loop)
    else:
        context.tool_approval = (None if approve is None
                                 else event_mod.Approval(approve, asyncio.get_running_loop()))
    return token_state


def _seconds(limit: float | None) -> str:
    """A clock on the run line: `15s`, or `off` for no limit."""
    return "off" if limit is None else f"{limit:g}s"


def _trace_banner(telemetry: Telemetry, request: ModelRequest, context: ToolContext, registry: ToolRegistry,
                  resolve_window: Callable[[], int | None]) -> None:
    """Show the run line: host, model, provider, window, output cap, effort, vision, memory, tools."""
    model, provider_id = request.model, request.provider_id
    if provider_id:
        provider_label = provider_id
        base = request.base_url or "provider-default"
    else:
        # model_client.resolve_model sends a model with no provider to its
        # base URL as an OpenAI-compatible endpoint.
        provider_label = "openai-compatible"
        base = request.base_url or "none"
    # ctx is the number that decides when compaction fires and how much room
    # is left to work in; max_out only bounds one reply. Showing the second
    # without the first invites reading 128000 as the window.
    ctx = compaction.configured_context_window(context.config, resolve_window)
    # key=none: no key, or the ENDPOINT_DEFAULT_KEY stand-in. The key itself
    # is never shown.
    dummy_key = _settings.knob(request.settings, "ENDPOINT_DEFAULT_KEY")
    key_state = "set" if request.api_key and request.api_key != dummy_key else "none"
    # The output cap and effort fields show what the built request carries.
    sent = model_client.wire_fields(model_client.build_request_params(
        model_id=model, provider_id=provider_id, provider_base_url=request.base_url,
        max_output_tokens=request.max_out, reasoning_effort=request.effort,
        thinking_budget=request.thinking_budget, provider_extra=request.extra,
        sampling=request.sampling, cache_key=request.cache_key, settings=request.settings))
    bits = [f"host={socket.gethostname()}",
            f"model={model}",
            f"key={key_state}",
            f"wire={routing.resolve_wire(provider_id).name}",
            f"provider={provider_label}",
            f"base={base}",
            f"ctx={ctx if ctx else 'unknown'}",
            f"max_out={sent.get('max_out', 'provider-default')}"]
    bits.extend(f"{name}={sent[name]}" for name in ("effort", "thinking", "DEEPSEEK_REASONING_TOKENS_MAX")
                if name in sent)
    clocks = retry.StreamLimits.from_settings(request.settings)
    bits.append(f"connect_timeout={_seconds(clocks.connect)}")
    bits.append(f"first_byte_timeout={_seconds(clocks.first_byte)}")
    bits.append(f"vision={'on' if context.vision_enabled else 'off'}")
    try:
        ntools = len(registry.openai_specs())
    except Exception:  # noqa: BLE001 — registry internals
        ntools = "?"
    bits.append(f"tools={ntools}")
    console.emit_event(telemetry, "run_line", fields=bits)


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
                      suppress_output: bool) -> tuple[list[tool_calls.PendingToolCall], str | None, ToolRegistry]:
    """Append the reply to the history and the convo. Returns the tool calls
    to run, the reply's incomplete reason, and the registry that authorizes
    the calls: the surface this model call saw."""
    result = reply.result
    text = result.text
    pending_calls = [
        tool_calls.PendingToolCall(id=call.id, name=call.name, arg_chunks=[call.arguments])
        for call in result.tool_calls
    ]
    provider_metadata, incomplete_reason = reply.provider_metadata, reply.incomplete_reason
    assistant_message_override: ai.messages.Message | None = None
    if incomplete_reason and pending_calls and tool_calls.incomplete_has_dangling_tool_args(pending_calls):
        notice = tool_calls.truncated_tool_call_notice(incomplete_reason, pending_calls) + "".join(
            held.hold_failed_call(context, pc.arguments(), None) for pc in pending_calls
            if tool_calls.canonical_tool_call_name(pc.name, registry) == "write")
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
        pending_calls, batch_stats = tool_calls.normalize_tool_call_batch(
            pending_calls,
            dispatch_registry,
            getattr(
                cfg,
                "max_tool_calls_per_message",
                _settings.default_value("REPLY_TOOL_CALLS_MAX"),
            ),
            context,
        )
        for received, tool in batch_stats.repaired:
            telemetry.event("tool_name_repaired", received=received, tool=tool)
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
                f'Tool-call batch limit reached: retained {batch_stats.retained} distinct calls and rejected {batch_stats.capped} beyond REPLY_TOOL_CALLS_MAX.'
            )
            batch_diagnostic_suffix = ("\n\n" if text else "") + diagnostic
            text += batch_diagnostic_suffix

    assistant_message = assistant_message_override or result.assistant_message
    if result.tool_calls:
        assistant_message = tool_calls.assistant_message_with_tool_calls(
            assistant_message,
            pending_calls,
            diagnostic_suffix=batch_diagnostic_suffix,
        )
        text = assistant_message.text
    record, assistant_message = history_record.assistant_record(
        assistant_message,
        text=text,
        tool_calls=[(pc.id, pc.name, pc.arguments()) for pc in pending_calls],
        reasoning_content=result.reasoning,
        # A rewritten reply keeps no signed reasoning, so no thinking blocks.
        reasoning_blocks=(getattr(result, "reasoning_blocks", None)
                          if assistant_message_override is None else None),
        provider_metadata=provider_metadata,
        incomplete_reason=incomplete_reason,
        provider_id=request.provider_id,
        model_id=request.model,
    )
    convo.append(memory.note_time(record), message=tool_calls.sanitize_assistant_message(assistant_message))
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


def _send_resume_nudge(convo: TurnConvo, *, n: int, limit: int,
                       incomplete_reason: str | None, model: str, telemetry: Telemetry,
                       suppress_output: bool) -> None:
    """Keep the partial reply and ask the model to carry on from where the
    cap cut it."""
    nudge = {"role": "user", "content": snippets.render("output-limit-resume"), "resume_nudge": True}
    convo.append(nudge)
    telemetry.event("max_output_resume", model=model, n=n, incomplete_reason=incomplete_reason)
    if not suppress_output:
        _notice(telemetry, msgs.MAX_OUTPUT_RESUMING, stderr=True, flush=True, n=n, limit=limit)


async def _record_tool_results(pending_calls: list[tool_calls.PendingToolCall], *, cfg: Config, trace: bool,
                               messages: list[dict], convo: TurnConvo, registry: ToolRegistry,
                               dispatch_registry: ToolRegistry, context: ToolContext,
                               error_tracker: tool_errors.ToolErrorTracker, telemetry: Telemetry,
                               events: TurnEvents) -> list[tuple[tool_calls.PendingToolCall, dict, Any]]:
    """Run the batch and append its results to the history and the convo.
    Returns the (call, arguments, result) records of the calls that ran.

    Each result becomes one history record (`tool_result_messages.tool_result_record`)
    and the convo is built from those records, so later turns and a resumed
    session send the same messages this turn does."""
    for pc in pending_calls:
        telemetry.event("tool_scheduled", tool=pc.called_name or pc.name, call_id=pc.id)
    for index, pc in enumerate(pending_calls):
        call = {"id": pc.id, "name": tool_calls.canonical_tool_call_name(pc.name, registry),
                "arguments": tool_calls.canonical_tool_args(pc.arguments())}
        emission = events.emit("tool_call", **call)
        refusal = event_mod.refusal_of(emission)
        if refusal and pc.validation_error is None:
            # An `on tool_call` handler refused it: the call never runs
            # and the model reads the refusal as its result.
            pending_calls[index] = replace(pc, validation_error=refusal, refused=True)
            telemetry.event("tool_call_refused", tool=pc.called_name or pc.name, call_id=pc.id, refusal=refusal)
        elif pc.validation_error is None and context.tool_approval is not None:
            refusal = await context.tool_approval.refusal(call)
            if refusal is not None:
                pending_calls[index] = replace(pc, validation_error=refusal, refused=True)
                telemetry.event("tool_call_refused", tool=pc.called_name or pc.name, call_id=pc.id, via="approval", refusal=refusal)
    # Tools are sync (subprocess, file I/O); leaf calls fan out to a worker
    # thread so the shared loop stays free while they execute. Fan-out (task /
    # named-agent) calls are awaited ON the loop instead, so a parent turn
    # never parks a dispatch thread its descendants need (see turn_dispatch.dispatch_batch).
    # A read that repeats an earlier one returns a stub naming it only
    # while that earlier result is still in the history the model sees.
    context.reads.keep_shown(_tool_results_in(messages))
    progress = turn_dispatch.DispatchProgress()
    context.turn_status.tool_begin([tool_calls.canonical_tool_call_name(pc.name, registry) for pc in pending_calls])
    try:
        dispatch_records = await turn_dispatch.dispatch_batch(
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
        capped = result_caps.cap_batch_results(
            [r for _, _, r in dispatch_records],
            getattr(cfg, "max_tool_results_per_turn_bytes", 0),
        )
        reconciled: list[tuple[tool_calls.PendingToolCall, dict, Any]] = []
        for (pc, args, old_result), new_result in zip(dispatch_records, capped, strict=True):
            turn_dispatch.reconcile_read_delivery(
                tool_calls.canonical_tool_call_name(pc.name, registry),
                args,
                old_result,
                new_result,
                context,
                pc.id,
            )
            reconciled.append((pc, args, new_result))
        dispatch_records = reconciled
        context.reads.settle()
        records: list[dict] = []
        for pc, _args, result_value in dispatch_records:
            canonical_pc = tool_calls.pending_with_name(pc, tool_calls.canonical_tool_call_name(pc.name, registry))
            events.emit(
                "tool_result",
                id=pc.id,
                name=canonical_pc.name,
                result=result_value,
            )
            record = tool_result_messages.tool_result_record(pc.id, canonical_pc.name, result_value)
            records.append(memory.note_time(record, progress.finished.get(pc.id)))
        # One append for the batch: its media follow all of its tool messages.
        convo.append(*records)
    return dispatch_records


def _steer_off_repeated_errors(due: list[tuple[str, int]], *, convo: TurnConvo,
                               telemetry: Telemetry) -> None:
    """Tell the model, in one message that joins the running turn, to stop
    retrying each tool in ``due`` (tool, repeats)."""
    text = "\n".join(snippets.render('repeated-tool-error', TOOL=name, COUNT=count)
                     for name, count in due)
    message = {"role": "user", "content": text, "steered": True}
    convo.append(memory.note_time(message))
    for name, count in due:
        telemetry.event("tool_error_steered", tool=name, repeats=count)


def _record_tool_error_limit(name: str, dispatch_records: list[tuple[tool_calls.PendingToolCall, dict, Any]], *,
                             registry: ToolRegistry, convo: TurnConvo, events: TurnEvents) -> None:
    """End the turn on an assistant message naming ``name``, the tool that
    repeated its error after being told to stop, and its last error."""
    last_error = next(
        (result_value
         for pc, _, result_value in reversed(dispatch_records)
         if tool_calls.canonical_tool_call_name(pc.name, registry) == name
         and isinstance(result_value, str) and not isinstance(result_value, Output)
         and result_value.startswith("ERROR")),
        "",
    )
    failure = f'ERROR: tool retry limit reached: {name} repeated the same error after being told to stop retrying it\n{last_error}'
    convo.append(memory.note_time({"role": "assistant", "content": failure}),
                 message=ai.messages.Message(role="assistant", parts=[ai.types.messages.TextPart(text=failure)]))
    events.emit("error", error=failure, retryable=False)
    events.end("tool_error_limit")


async def _take_steer(steer: Callable[[], dict | None | Awaitable[dict | None]], convo: TurnConvo,
                      telemetry: Telemetry, suppress_output: bool) -> None:
    """Append the message ``steer`` returns, if any, at this tool boundary."""
    steered = steer()
    if inspect.isawaitable(steered):
        steered = await steered
    if steered is not None:
        convo.append(memory.note_time(steered))
        telemetry.event("steered", message_index=len(convo.messages) - 1)
        if not suppress_output:
            _notice(telemetry, msgs.STEERED, flush=True)


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
             event_sink: Callable[[str, dict], None] | None = None,
             approve: event_mod.Approve | event_mod.Approval | None = None,
             *, skill_catalog: skills.SkillCatalog | None = None,
             state_dir: Path | None = None,
             checkpoint: Callable[[], None] | None = None) -> None:
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

    ``approve`` (`events.Approve`) is awaited for each tool call the
    `tool_call` handlers let through, one call at a time, before the batch
    runs. It returns None to let the call run, or the line the model reads as
    the refused call's result. Subagents started in this turn and tool calls
    from kernel cells answer to it too. None runs every call.

    ``skill_catalog`` supplies this turn's skill discovery policy and the
    built-in skill tool's catalog. None discovers skills from the usual roots.

    ``state_dir`` is the root of this session's persistent undo store. None
    uses the usual state root.

    ``checkpoint`` is called, off the loop, after each batch of tool results is
    recorded: `messages` then holds every assistant message with all its
    results, so the caller can save it and a killed process keeps the turn.

    Provider overrides let the REPL /prompt mode switch endpoint without
    reloading config; unset values fall back to the Config values.
    """
    if sampling is None:
        # The sampling of the model this turn runs, resolved like every other.
        sampling = login_defaults.sampling(_overridden(
            cfg, model_override, provider_id_override, provider_base_url_override))
    cfg = _with_login_defaults(cfg, model_override, provider_id_override, provider_base_url_override)
    model_metadata.bind_settings(getattr(cfg, "settings", None))
    request = _turn_request(
        cfg, model_override=model_override, provider_id_override=provider_id_override,
        provider_base_url_override=provider_base_url_override,
        provider_api_key_override=provider_api_key_override,
        reasoning_effort_override=reasoning_effort_override, max_output_override=max_output_override,
        sampling=sampling,
    )
    model, provider_id = request.model, request.provider_id
    convo = TurnConvo(system, messages, provider_id=provider_id, model=model,
                      vision=vision_enabled_for_model(model, getattr(cfg, "settings", None)))
    base_registry = tool_registry or T.stock_registry()
    alias_map = tool_calls.resolve_alias_profile(getattr(cfg, "settings", {}) or {}, model, provider_id, base_registry)
    active_context = tool_context or T.STOCK_CONTEXT
    owns_mcp_host = mcp_host is None
    if owns_mcp_host and getattr(cfg, "mcp", None) is not None and getattr(cfg.mcp, "servers", ()):
        from .mcp.host import MCPHost

        mcp_host = MCPHost(cfg.mcp, telemetry=telemetry, paranoid_mode=getattr(cfg, "paranoid_mode", "off"))
    # A fresh registry rechecks current policy and aliases while restoring the
    # session's visibility. Marks survive compaction and process restarts.
    # A tool that cannot run here is left off the surface the model sees.
    runnable_registry, left_out = base_registry.runnable(active_context)
    for name, reason in left_out.items():
        telemetry.event("tool_unpublished", tool=name, reason=reason)
    active_registry = runnable_registry.aliased(alias_map).lazy_surface(
        active_context.cwd, mcp_host=mcp_host, disabled_skills=cfg.skills_disabled,
        skill_catalog=skill_catalog)
    active_context.skill_catalog = skill_catalog
    surface = SurfaceJournal(getattr(cfg, "session_file", None), agent_id=cfg.agent_id,
                             cwd=active_context.cwd, registry=active_registry)
    token_state = _prepare_turn_context(active_context, cfg, request, active_registry, event_hooks, approve,
                                       state_dir=state_dir)
    turn_approval = active_context.tool_approval
    turn_status = active_context.turn_status

    events = TurnEvents(event_hooks, event_sink, telemetry, model=model, provider_id=provider_id)
    if mcp_host is not None:
        mcp_host.telemetry = telemetry
        mcp_host.event_sink = events.emit
        mcp_host.paranoid_mode = getattr(cfg, "paranoid_mode", "off")
    events.start(len(messages))

    trace = trace_override if trace_override is not None else cfg.trace
    resolve_window = functools.partial(context_window.resolve_context_window, model, provider_id, request.base_url)
    if trace:
        _trace_banner(telemetry, request, active_context, active_registry, resolve_window)

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
        alias=functools.partial(tool_calls.aliased_tool_specs, alias_map=alias_map), mcp_host=mcp_host,
        call_stats=call_stats, trace=trace, suppress_output=suppress_output,
    )
    error_tracker = tool_errors.ToolErrorTracker.from_settings(getattr(cfg, "settings", None))

    output_token = output.set_sink(output.sink_of(telemetry))
    net_role_token = stream_transport.set_role(
        active_context.net_label, agent=cfg.agent_id, status=turn_status, retries=True,
    )
    net_output_token = stream_transport.set_output(
        lambda level, line, fallback: console.emit_event(telemetry, "network", level=level, line=line, fallback=fallback))
    usage_token = usage_mod.start(usage_mod.Meter(
        (usage_mod.session_key(getattr(cfg, "session_file", None), active_context),
         *getattr(active_context, "usage_chain", ())),
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
                _notice(telemetry, msgs.RETRY_BUDGET_EXHAUSTED)
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
                _notice(telemetry, msgs.RESPONSE_INCOMPLETE, stderr=True, flush=True, reason=incomplete_reason)
            if resuming:
                resumes_sent += 1
                _send_resume_nudge(convo, n=resumes_sent, limit=limits.max_output_resumes,
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
            if checkpoint is not None:
                await asyncio.to_thread(checkpoint)
            exhausted = error_tracker.exhausted()
            if exhausted is not None:
                _record_tool_error_limit(exhausted, dispatch_records, registry=active_registry,
                                         convo=convo, events=events)
                return
            due = error_tracker.steer_due()
            if due:
                _steer_off_repeated_errors(due, convo=convo, telemetry=telemetry)
            if steer is not None and iteration + 1 < cfg.max_tool_iterations:
                await _take_steer(steer, convo, telemetry, suppress_output)

        _notice(telemetry, msgs.MAX_ITERATIONS, limit=cfg.max_tool_iterations)
        events.end("max_iterations")
    except BaseException as _turn_exc:  # noqa: BLE001
        if turn_approval is not None:
            turn_approval.cancel()
        # turn_start is emitted unconditionally, so every exit pairs it with
        # exactly one turn_end: cancellation (CancelledError /
        # KeyboardInterrupt) ends "cancelled", an unexpected Exception ends
        # "error", and an exit that already ended the turn adds nothing.
        # Either way the answer streamed so far is finished where it stands:
        # left open, the next reply would take over its place on the screen.
        sink.close()
        if not isinstance(_turn_exc, Exception):
            sink.commit_partial(messages)
            if not events.ended:
                events.end("cancelled")
        else:
            stream_transport.report_held_failure()
            if not events.ended:
                events.end("error", error=f"{type(_turn_exc).__name__}: {_turn_exc}")
        raise
    finally:
        sink.close_reasoning()
        console.settle_tool_trace(telemetry)
        usage_mod.stop(usage_token)
        stream_transport.reset_role(net_role_token)
        stream_transport.reset_output(net_output_token)
        output.reset_sink(output_token)
        turn_status.reset()
        if owns_mcp_host and mcp_host is not None:
            await mcp_host.close()
