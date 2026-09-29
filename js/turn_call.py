"""One model call of a turn, carried through what providers do to it.

`ModelCaller.call` sends the turn's convo and returns the reply. On the way
it retries retryable errors under the retry budget (honouring Retry-After),
sheds history when the provider refuses the request as too long or shows it
cut the input to fit (a silent overflow), resends once with a larger output
cap a reply cut off by its cap, and resends once without signed reasoning a
request whose replayed signatures the provider refused. A fatal error ends
the turn and propagates. None means the attempts ran out.

The call reaches the model only through `model_client.stream_model_async`,
looked up on the module at each call.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import ai

from . import colors as C
from . import compaction
from . import context_budget
from . import display
from . import memory
from . import messages as msgs
from . import model_client
from . import model_metadata
from . import retry
from . import settings as _settings
from . import stream_transport
from . import tool_args
from . import usage as usage_mod
from .retry import backoff as _backoff, retry_after_seconds
from .sampling import Sampling
from .turn_budget import TurnBudget, TurnConvo
from .turn_stream import StreamSink, TurnEvents


def dangling_arguments(arguments: Iterable[str]) -> bool:
    """Some tool call's arguments are not a whole JSON object."""
    return any(not tool_args.is_json_object(raw) for raw in arguments)


def cut_off_by_cap(incomplete_reason: str | None, arguments: list[str]) -> bool:
    """A reply stopped by its output-token cap with nothing runnable: no tool
    call, or one whose arguments the cut left unfinished. ``arguments`` are
    the raw arguments of the reply's tool calls."""
    return (
        bool(incomplete_reason)
        and compaction.is_max_output_incomplete(incomplete_reason)
        and (not arguments or dangling_arguments(arguments))
    )


def escalated_max_output(cut_at: int | None, ceiling: int | None, escalation: int,
                         room: int | None) -> int | None:
    """The larger output cap for resending a cut-off reply, or None when there
    is none. `escalation` is held to the model's known output ceiling and to
    `room`, what the window has left after the prompt; it must exceed
    `cut_at`, the cap the reply was cut at."""
    if escalation <= 0:
        return None
    target = escalation if ceiling is None else min(escalation, ceiling)
    if room is not None:
        target = min(target, room)
    if target <= 0 or (cut_at is not None and target <= cut_at):
        return None
    return target


def silent_overflow(usage: Any, context_window: int | None, *, cut_by_cap: bool) -> int | None:
    """The prompt tokens of a reply whose input the provider cut to fit its
    window without an error, else None. Two signs: prompt tokens over the
    window, or a reply cut by its cap with no output and a prompt filling 99%
    of the window. The SDK's input_tokens already counts cache reads."""
    if not context_window or context_window <= 0 or usage is None:
        return None
    reported = context_budget.usage_from_provider(usage)
    prompt = reported.prompt_tokens
    if prompt > context_window:
        return prompt
    if cut_by_cap and not reported.output_tokens and prompt >= context_window * 0.99:
        return prompt
    return None


@dataclass(frozen=True)
class CallLimits:
    """The resilience settings a turn's model calls run under: the retry
    budget, the stream idle watchdog, the escalated output cap for one resend
    of a cut-off reply (0: none), and how many resume nudges a turn may send
    after replies cut off by their cap."""

    retry: retry.Budget
    stream_idle: float | None
    max_output_escalation: int
    max_output_resumes: int

    @classmethod
    def from_settings(cls, settings: dict | None) -> CallLimits:
        return cls(
            retry=retry.Budget.from_settings(settings),
            stream_idle=retry.idle_seconds(settings),
            max_output_escalation=int(_settings.knob(settings, "runtime.max_output_escalation") or 0),
            max_output_resumes=int(_settings.knob(settings, "runtime.max_output_resumes") or 0),
        )


@dataclass(frozen=True)
class ModelRequest:
    """What every model call of a turn sends besides the convo and the tools."""

    model: str
    provider_id: str | None
    base_url: str | None
    api_key: str | None
    effort: Any
    max_out: int | None
    thinking_budget: int | None = None
    headers: Any = None
    extra: Any = None
    sampling: Sampling | None = None
    cache_key: str | None = None


@dataclass
class ModelReply:
    result: model_client.ModelStreamResult
    finish: str | None
    provider_metadata: Any
    incomplete_reason: str | None
    # The SDK tools the reply's request carried.
    ai_tools: Any
    # The reply's usage counts history that silent-overflow recovery has
    # since shed.
    usage_stale: bool


class ModelCaller:
    """Makes the model calls of one turn. ``registry`` lists the tool specs
    and ``alias`` renames them as the request sends them. Overflow rounds and
    the one escalated resend are counted across the turn's calls."""

    def __init__(self, request: ModelRequest, limits: CallLimits, *, convo: TurnConvo, budget: TurnBudget,
                 sink: StreamSink, events: TurnEvents, telemetry: Any, context: Any, turn_status: Any,
                 registry: Any, alias: Callable[[list[dict]], list[dict]], mcp_host: Any = None,
                 call_stats: list[dict] | None = None, trace: bool = False, suppress_output: bool = False):
        self._request = request
        self._limits = limits
        self._convo = convo
        self._budget = budget
        self._sink = sink
        self._events = events
        self._telemetry = telemetry
        self._context = context
        self._status = turn_status
        self._registry = registry
        self._alias = alias
        self._mcp_host = mcp_host
        self._call_stats = call_stats
        self._trace = trace
        self._suppress = suppress_output
        self._trace_sink = getattr(telemetry, "trace_sink", None)
        self._overflow_rounds = 0
        self._escalated = False

    async def call(self, *, phase: str) -> ModelReply | None:
        """One model call. ``phase`` names the budget check before it:
        "preflight" before the turn's first durable side effect, else "midturn"."""
        request, limits, telemetry, context = self._request, self._limits, self._telemetry, self._context
        model, max_out = request.model, request.max_out
        reply: ModelReply | None = None
        budget_checked = False
        transport_retries = 0
        # The cap this call runs under: max_out, or the escalated cap for the
        # one resend of a reply cut off by max_out.
        call_max_out = max_out
        usage_stale = False
        signed_reasoning_dropped = False
        # Retries, overflow rounds (a provider rejection or a silent
        # overflow), one escalated resend and its fallback, and one resend
        # without signed reasoning.
        for attempt in range(limits.retry.attempts + 1 + compaction.MAX_OVERFLOW_ROUNDS + 3):
            t0 = time.time()
            self._sink.clear()
            try:
                if self._mcp_host is not None:
                    await self._mcp_host.before_model_call()
                specs = self._alias(self._registry.openai_specs())
                if not budget_checked:
                    await self._budget.fit(phase=phase, specs=specs)
                    budget_checked = True
                ai_tools = model_client.tool_specs_to_ai_tools(specs) if specs else None
                result = await self._stream(specs, ai_tools, call_max_out)
                reply = self._reply(result, ai_tools, call_max_out, t0, usage_stale)
                cut_by_cap = cut_off_by_cap(reply.incomplete_reason,
                                            [call.arguments for call in result.tool_calls])
                # Reply text already on the screen or stdout. Sending the
                # request again would print a second reply after it.
                shown = self._sink.shown
                window = self._budget.provider_window()
                silent = silent_overflow(result.usage, window, cut_by_cap=cut_by_cap)
                if silent is not None and self._overflow_rounds < compaction.MAX_OVERFLOW_ROUNDS:
                    # The provider took more input than the window holds, so
                    # it cut the input: shed history and ask again. A reply
                    # already shown is kept, and the shed history serves the
                    # next request.
                    self._sink.close()
                    self._overflow_rounds += 1
                    telemetry.event("context_overflow_silent", model=model, prompt_tokens=silent,
                                    attempt=attempt, round=self._overflow_rounds, kept_reply=shown)
                    if await self._recover(compaction.SilentOverflowError(silent, window)):
                        if not shown:
                            continue
                        usage_stale = reply.usage_stale = True
                if not self._escalated and cut_by_cap and not shown and (
                        escalated := self._escalation(result.usage, call_max_out, window)) is not None:
                    # Cut off by its cap: send the same request once more
                    # with room to finish, before any resume nudge.
                    self._sink.close()
                    self._escalated = True
                    telemetry.event("max_output_escalated", model=model,
                                    max_output_tokens=call_max_out, escalated_to=escalated)
                    stream_transport.say_for_caller(
                        2, msgs.MAX_OUTPUT_ESCALATED.text(before=call_max_out or "default", after=escalated))
                    call_max_out = escalated
                    continue
                return reply
            except ai.ProviderAPIError as e:
                # Finish any partially streamed text before we retry or abort,
                # so the next attempt's output starts on its own line.
                self._sink.close()
                if call_max_out != max_out and not e.is_retryable:
                    # The provider refused the escalated cap, often as a
                    # context-length error because prompt plus cap passes
                    # the window. The configured cap fit before: carry on
                    # at it, where resume nudges take over.
                    telemetry.event("max_output_escalation_rejected", model=model,
                                    error=f"{type(e).__name__}: {e}", escalated_to=call_max_out)
                    call_max_out = max_out
                    continue
                if (
                    compaction.is_context_overflow_error(e)
                    and self._overflow_rounds < compaction.MAX_OVERFLOW_ROUNDS
                ):
                    self._overflow_rounds += 1
                    telemetry.event(
                        "context_overflow_error",
                        model=model,
                        error=f"{type(e).__name__}: {e}",
                        attempt=attempt,
                        round=self._overflow_rounds,
                    )
                    if await self._recover(e):
                        continue
                if not signed_reasoning_dropped and model_client.is_signed_reasoning_rejection(e):
                    # The provider refused a replayed signature (an edit js
                    # made before it, or a system or tool change): replay
                    # the history without signed reasoning, once.
                    signed_reasoning_dropped = True
                    dropped = memory.drop_signed_reasoning(self._convo.messages)
                    telemetry.event("signed_reasoning_dropped", model=model, messages=dropped,
                                    error=f"{type(e).__name__}: {e}")
                    self._convo.rebuild()
                    compaction.history_rewritten(context)
                    continue
                if e.is_retryable:
                    telemetry.event("retriable_error", model=model,
                                    error=f"{type(e).__name__}: {e}", attempt=attempt)
                    wait = retry_after_seconds(e)
                    too_long = limits.retry.too_long(wait)
                    if transport_retries >= limits.retry.attempts or too_long:
                        if too_long:
                            telemetry.event("retry_after_too_long", model=model,
                                            retry_after=wait, limit=limits.retry.max_wait)
                        self._fail(e, retryable=True)
                        raise
                    delay = wait if wait is not None else _backoff(transport_retries)
                    transport_retries += 1
                    retry.announce(transport_retries, limits.retry, delay, e)
                    await asyncio.sleep(delay)
                else:
                    telemetry.event("fatal_error", model=model, error=f"{type(e).__name__}: {e}")
                    self._fail(e, retryable=False)
                    raise
            except (ai.ConfigurationError, ai.InstallationError, ai.UnsupportedProviderError, ValueError) as e:
                self._sink.close()
                telemetry.event("fatal_error", model=model, error=f"{type(e).__name__}: {e}")
                self._fail(e, retryable=False)
                raise
        return None

    async def _stream(self, specs: list[dict], ai_tools: Any, max_out: int | None) -> Any:
        request, convo = self._request, self._convo
        self._events.emit(
            "prompt",
            model=request.model,
            provider_id=request.provider_id,
            message_count=len(convo.ai),
            tool_count=len(specs),
            tool_names=[spec["function"]["name"] for spec in specs],
        )
        pending = model_client.stream_model_async(
            model_id=request.model,
            provider_id=request.provider_id,
            provider_base_url=request.base_url,
            provider_api_key=request.api_key,
            messages=convo.ai,
            tools=ai_tools,
            max_output_tokens=max_out,
            reasoning_effort=request.effort,
            on_text=self._sink.text,
            on_reasoning=self._sink.reasoning,
            thinking_budget=request.thinking_budget,
            provider_headers=request.headers,
            provider_extra=request.extra,
            sampling=request.sampling,
            trace_request=self._trace_sink is not None,
            trace_sink=self._trace_sink,
            trace_request_schemas=convo.schemas,
            trace_request_from=convo.sent,
            cache_key=request.cache_key,
            stream_idle_seconds=self._limits.stream_idle,
        )
        if self._trace_sink is not None:
            convo.traced()
        # Await the native async primitive; tolerate a sync override (a test
        # stub patched onto stream_model_async that returns a result directly)
        # so the seam accepts either shape.
        result = await pending if inspect.isawaitable(pending) else pending
        self._sink.close(getattr(result.usage, "reasoning_tokens", None))
        return result

    def _reply(self, result: Any, ai_tools: Any, max_out: int | None, t0: float,
               usage_stale: bool) -> ModelReply:
        """Read the reply, and charge and report the call."""
        model, provider_id, context = self._request.model, self._request.provider_id, self._context
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
        usage = result.usage
        usage_mod.record(usage, model=model, provider_id=provider_id)
        context.last_prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        context.last_cached_tokens = int(getattr(usage, "cache_read_tokens", 0) or 0) if usage else 0
        context.last_incomplete_reason = incomplete_reason
        context.last_max_output_tokens = max_out
        cache_break = compaction.note_response(
            context, model_key=f"{provider_id}/{model}",
            cache_read=context.last_cached_tokens if usage else None, now=time.time(),
        )
        if cache_break is not None:
            self._telemetry.event("prompt_cache_break", model=model, line=cache_break)
            stream_transport.say_for_caller(2, cache_break)
        n_calls = len(result.tool_calls)
        self._telemetry.event("turn_complete", model=model,
                              latency_ms=int((time.time() - t0) * 1000),
                              finish_reason=finish, n_tool_calls=n_calls,
                              incomplete_reason=incomplete_reason,
                              prompt_tokens=context.last_prompt_tokens,
                              cached_tokens=context.last_cached_tokens)
        out_tokens = 0
        if usage:
            out_tokens = int(getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0)
        context.last_output_tokens = out_tokens
        self._status.settle(out_tokens)
        if self._call_stats is not None:
            # Stream-isolated numbers (model_client clocks `ai.stream` itself,
            # free of run_turn's setup/bookkeeping) for honest tok/s and TTFT.
            stream_s = result.elapsed_s or (time.time() - t0)
            self._call_stats.append({
                "ttft_s": result.first_token_s,
                "stream_s": result.elapsed_s,
                "output_tokens": out_tokens,
                "prompt_tokens": context.last_prompt_tokens,
                "cached_tokens": context.last_cached_tokens,
                "tok_per_s": (out_tokens / stream_s) if stream_s > 0 else 0.0,
                "finish_reason": finish,
                "n_tool_calls": n_calls,
            })
        net = stream_transport.net_level()
        label = context.net_label
        if (net >= 3 and (label or not self._suppress)) if net is not None else self._trace:
            elapsed = time.time() - t0
            tps = (out_tokens / elapsed) if elapsed > 0 else 0.0
            cache = ""
            if context.last_prompt_tokens > 0 and context.last_cached_tokens > 0:
                cache = f"  cache {100.0 * context.last_cached_tokens / context.last_prompt_tokens:.0f}%"
            ttft = f"  ttft {int(result.first_token_s * 1000)}ms" if result.first_token_s is not None else ""
            stats = msgs.CALL_STATS.text(
                ms=int(elapsed * 1000), finish=finish, tool_calls=n_calls,
                tokens=out_tokens, tps=tps, ttft=ttft, cache=cache)
            print(f"{display.CHROME}{label + ': ' if label else ''}{stats}{C.RESET}", flush=True)
        return ModelReply(result=result, finish=finish, provider_metadata=provider_metadata,
                          incomplete_reason=incomplete_reason, ai_tools=ai_tools, usage_stale=usage_stale)

    def _escalation(self, usage: Any, max_out: int | None, window: int | None) -> int | None:
        """The cap to resend a reply cut off at ``max_out`` with, or None."""
        request = self._request
        if not model_client.sends_max_output(request.provider_id):
            return None
        prompt_tokens = context_budget.usage_from_provider(usage).prompt_tokens
        out_tokens = self._context.last_output_tokens
        return escalated_max_output(
            max_out if max_out is not None else out_tokens or None,
            model_metadata.resolve_max_output(request.model, request.provider_id),
            self._limits.max_output_escalation,
            window - prompt_tokens if window and prompt_tokens else None,
        )

    async def _recover(self, error: BaseException) -> bool:
        tools = self._registry.openai_specs()
        return await self._budget.recover_overflow(
            error, overflow_round=self._overflow_rounds, tools=tools, specs=self._alias(tools),
        )

    def _fail(self, error: BaseException, *, retryable: bool) -> None:
        self._events.emit("error", error=f"{type(error).__name__}: {error}", retryable=retryable)
        self._events.end("error")
