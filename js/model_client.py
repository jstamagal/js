"""Single import boundary for the Vercel AI Python SDK (`ai`).

This is the only production module that imports `ai`. It adapts the SDK's
async, part-based API to the synchronous, dict-based runtime used by `js`.
This is the canonical provider boundary for the migration.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from typing import Any

import ai
import ai.models
import ai.types.messages
import ai.types.tools
import ai.types.usage
from ai.models.core import params as ai_params
from ai.providers.openai.protocol import OpenAIChatCompletionsProtocol

from . import (
    debug_trace,
    paranoid,
    provider_errors,
    providers,
    reasoning,
    request_params,
    retry,
    routing,
    stream_protocols,
    stream_transport,
    tool_result_messages,
)
from .history_record import (
    with_reasoning_blocks,
)
from .sampling import Sampling


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelStreamResult:
    text: str
    tool_calls: list[ModelToolCall]
    reasoning: str
    usage: ai.types.usage.Usage | None
    finish_reason: str  # "tool_calls" when tool_calls is non-empty, otherwise "stop"; incomplete streams use "incomplete:<reason>".
    assistant_message: ai.messages.Message
    # Wall-clock measured around `ai.stream` itself (isolated from run_turn's
    # bookkeeping). first_token_s is time-to-first-*text*-token: js collects
    # reasoning separately from on_text, and a tool-only turn streams no text,
    # so it is None when the model emitted no visible text. elapsed_s is the full
    # stream duration. Both default to 0/None so older constructors stay valid.
    first_token_s: float | None = None
    elapsed_s: float = 0.0
    provider_metadata: dict[str, Any] | None = None
    incomplete_reason: str | None = None
    # Claude's thinking blocks as CLIProxyAPI returns them on the OpenAI wire
    # (`reasoning_blocks`), whole and in order; None when the reply had none.
    reasoning_blocks: list[dict[str, Any]] | None = None


# Headers the request body decides; a configured one is dropped before the
# SDK sees it.
_FRAMING_HEADERS = frozenset({"content-type", "content-length", "transfer-encoding", "host"})


def _without_framing(headers: dict[str, str] | None) -> dict[str, str]:
    return {k: v for k, v in (headers or {}).items() if k.lower() not in _FRAMING_HEADERS}


def with_extra_headers(
    headers: dict[str, str] | None, extra: dict[str, str] | None,
) -> dict[str, str]:
    """``headers`` with ``extra`` laid on top. A name in both (compared
    without case, as HTTP does) takes ``extra``'s value."""
    merged = dict(headers or {})
    for name, value in (extra or {}).items():
        for existing in [key for key in merged if key.lower() == name.lower()]:
            del merged[existing]
        merged[name] = value
    return merged


def turn_headers(headers: dict[str, str] | None, cfg: Any) -> dict[str, str]:
    """The headers a turn's model request sends: the login's ``headers``,
    then ``cfg``'s EXTRA_HEADERS, then what PARANOID_MODE adds."""
    merged = with_extra_headers(headers, getattr(cfg, "extra_headers", None))
    return with_extra_headers(merged, paranoid.wire_headers(getattr(cfg, "paranoid_mode", "off")))


_model_factory: contextvars.ContextVar[Callable[..., ai.Model] | None] = contextvars.ContextVar(
    "js_model_factory", default=None,
)


@contextmanager
def using_model_factory(factory: Callable[..., ai.Model]) -> Iterator[None]:
    """Build streamed models with ``factory`` in this scope and its child tasks.

    The factory takes the same arguments as ``resolve_model``: positional
    ``model_id`` and keyword provider id, base URL, API key and headers.
    Each call transfers a model to js, which closes its provider after streaming.
    Turns and summary compaction share this scope; nested scopes restore it.
    """
    token = _model_factory.set(factory)
    try:
        yield
    finally:
        _model_factory.reset(token)


def resolve_model(
    model_id: str,
    *,
    provider_id: str | None,
    provider_base_url: str | None,
    provider_api_key: str | None,
    provider_headers: dict[str, str] | None = None,
) -> ai.Model:
    """Return an `ai.Model`, optionally bound to an explicit js provider.

    ``provider_id`` is a user-facing js id; `routing.resolve_wire` translates
    it to the SDK and protocol, and the model id is passed through verbatim.
    With no provider, an explicit base URL is a deliberate endpoint choice:
    an anonymous OpenAI-compatible endpoint on chat completions, like every
    custom base. `ai.get_model` takes neither a base URL nor a key and would
    route a bare id to the Vercel gateway.
    """
    wire = routing.resolve_wire(provider_id, base_url=provider_base_url)
    if provider_id is None and not provider_base_url:
        # With neither a provider nor a base URL, riding the SDK's own env keys
        # (AI_GATEWAY_API_KEY, OPENAI_API_KEY, ...) is exactly the env-farming
        # the login gate forbids: fail with the same actionable not-logged-in
        # message instead.
        raise routing.ProviderNotLoggedInError(routing.unconfigured_model_message(model_id))
    # No client-side allowlist gate: ``allowed_models`` is a curated hint used to
    # filter noisy /models listings, NOT an authority on what the endpoint serves.
    # It goes stale the moment a provider ships a new id (e.g. opencode-go's
    # glm-5.2), so refusing here only blocks a model the server happily answers.
    # Let the request through; the provider is the one source of truth and will
    # 400 with its own message if the id is genuinely unknown.
    if wire.provider is not None:
        providers.assert_endpoint_configured(wire.provider, provider_base_url)

    headers = {**(wire.provider.headers if wire.provider is not None else {}), **(provider_headers or {})}
    # ai>=0.2.1 flipped the openai SDK's default wire to the Responses API; every
    # chat-completions endpoint js targets through the openai SDK (opencode-go,
    # mimo, ollama, llama.cpp, custom OpenAI-compatible bases) 404s on /responses.
    # Pin chat-completions for those; only the explicit Responses transport keeps
    # the new default.
    protocol = None
    if wire.sdk == "anthropic":
        protocol = stream_protocols.AnthropicThinkingProtocol(explicit_prompt_cache=wire.provider_id != "anthropic")
    elif wire.responses:
        protocol = stream_protocols.ResponsesProtocol()
    elif wire.sdk == "openai":
        protocol = (
            # Only a CLIProxyAPI route sends `reasoning_blocks`: its plugin
            # takes them; a direct endpoint would not know the field.
            stream_protocols.ReasoningContentProtocol(reasoning_blocks=wire.transport == "cliproxyapi")
            if wire.reasoning_content
            else OpenAIChatCompletionsProtocol()
        )
    provider = ai.get_provider(
        wire.sdk,
        base_url=provider_base_url,
        # With no provider a missing key goes out as "" (a keyless local
        # server), never as the SDK's env key.
        api_key=provider_api_key if provider_id is not None else provider_api_key or "",
        headers=_without_framing(headers) or None,
        protocol=protocol,
    )
    return ai.Model(id=model_id, provider=provider)


def tool_specs_to_ai_tools(specs: list[dict]) -> list[ai.types.tools.Tool]:
    """Convert OpenAI-shaped tool schemas to ``ai.types.tools.Tool``.

    ``specs`` come from the harness registry's ``openai_specs()`` and have
    shape ``{"type": "function", "function": {"name": ..., "description": ...,
    "parameters": ...}}``.
    """
    tools: list[ai.types.tools.Tool] = []
    for spec in specs:
        if spec.get("type") != "function":
            raise ValueError(f"unsupported tool spec type: {spec.get('type')!r}")
        fn = spec.get("function", {})
        name = fn.get("name")
        if not name:
            raise ValueError("tool spec missing function.name")
        spec_obj = ai.types.tools.ToolSpec(
            description=fn.get("description") or "",
            params=fn.get("parameters") or {"type": "object"},
        )
        tools.append(
            ai.types.tools.Tool(kind="function", name=name, spec=spec_obj)
        )
    return tools


def _lay_headers(sdk_client: Any, headers: dict[str, str]):
    """A request hook that settles each outgoing request's headers once the
    SDK has added its own: ``headers`` (the login's, EXTRA_HEADERS) replace
    the SDK's defaults under any spelling, then the SDK's auth header from
    the login's key replaces any configured one. Each name goes out once."""
    auth = {name: value for name, value in dict(getattr(sdk_client, "auth_headers", None) or {}).items()
            if value}

    async def lay(request: Any) -> None:
        for name, value in headers.items():
            request.headers[name] = value
        for name, value in auth.items():
            request.headers[name] = value

    return lay


def _sdk_client(model: ai.Model) -> Any:
    """The OpenAI or Anthropic SDK client of ``model``'s provider, built on
    first use; None for any other provider. Building the first one in a
    process imports the provider SDK, about a second of blocking work."""
    from ai.providers.anthropic.provider import AnthropicCompatibleProvider
    from ai.providers.openai.provider import OpenAICompatibleProvider

    if isinstance(model.provider, (OpenAICompatibleProvider, AnthropicCompatibleProvider)):
        return model.provider.sdk_client
    return None


def _http_client(model: ai.Model) -> Any:
    """The provider's HTTP client when it exposes per-request event hooks."""
    sdk_client = _sdk_client(model)
    if sdk_client is not None:
        return sdk_client._client
    return None


@asynccontextmanager
async def _open_stream(
    *,
    model: ai.Model,
    messages: list[ai.messages.Message],
    tools: Sequence[ai.types.tools.Tool] | None,
    params: ai_params.InferenceRequestParams | None = None,
    on_request: Callable[[], None] | None = None,
    on_bytes: Callable[[], None] | None = None,
    connect_timeout: float | None = None,
) -> AsyncIterator[ai.models.Stream]:
    """Open an SDK stream through one js-owned, patchable boundary.

    ai 0.4 removed the public ``executor=`` argument from :func:`ai.stream`.
    Keeping the test seam here lets offline tests supply a public
    :class:`ai.models.Stream` without reaching into SDK internals.

    ``on_request`` is called as each HTTP request is sent and ``on_bytes`` for
    each response body chunk the transport reads. Each request's handshake is bounded by
    ``connect_timeout`` (None: no limit) and its reads by nothing: the
    first-byte and idle clocks of `_stream_async` bound those silences.
    The OpenAI and Anthropic SDK clients retry nothing themselves: every retry
    runs under js's budget (js/retry.py).
    """
    async with AsyncExitStack() as cleanup:
        sdk_client = _sdk_client(model)
        if sdk_client is not None:
            sdk_client.max_retries = 0
        http_client = _http_client(model)
        if http_client is not None:
            await cleanup.enter_async_context(stream_transport.own_responses(
                http_client, stream_transport.current_call(),
                on_request=on_request, on_bytes=on_bytes,
                timeout={"connect": connect_timeout, "read": None},
            ))
        if sdk_client is not None:
            headers = dict(getattr(model.provider, "headers", None) or {})
            if headers:
                hooks = sdk_client._client.event_hooks["request"]
                lay = _lay_headers(sdk_client, headers)
                hooks.append(lay)
                cleanup.callback(hooks.remove, lay)
        async with ai.stream(model=model, messages=messages, tools=tools, params=params) as stream:
            yield stream


def _usage_from_stream(stream: ai.models.Stream) -> ai.types.usage.Usage | None:
    usage = stream.usage
    if usage is None:
        return None
    if isinstance(usage, ai.types.usage.Usage):
        return usage
    # Defensive: the SDK always returns Usage, but tolerate dict-like passthrough.
    try:
        return ai.types.usage.Usage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            reasoning_tokens=getattr(usage, "reasoning_tokens", None),
            cache_read_tokens=getattr(usage, "cache_read_tokens", None),
            cache_write_tokens=getattr(usage, "cache_write_tokens", None),
            raw=getattr(usage, "raw", None),
        )
    except Exception:
        return None


# The reasoning callback of the `stream_model_async` call running in this
# context. `stream_model_async` sets it around its `_stream_async` call and
# `_stream_async` routes each `ReasoningDelta` chunk to it; concurrent streams
# run in separate asyncio tasks, so each sees its own value.
_reasoning_sink: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "js_reasoning_sink", default=None,
)

# The clocks of the `stream_model_async` call running in this context.
_limits: contextvars.ContextVar[retry.StreamLimits] = contextvars.ContextVar(
    "js_stream_limits", default=retry.StreamLimits(),
)

# The response events that carry model output.
_OUTPUT_EVENTS = (ai.events.TextDelta, ai.events.ReasoningDelta, ai.events.ToolDelta)


async def _stream_async(
    model: ai.Model,
    messages: list[ai.messages.Message],
    tools: list[ai.types.tools.Tool] | None,
    params: ai_params.InferenceRequestParams | None,
    on_text: Callable[[str], None],
) -> ModelStreamResult:
    on_reasoning = _reasoning_sink.get()
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
    }
    if params is not None:
        kwargs["params"] = params
    start = time.perf_counter()
    first_token_s: float | None = None
    stream_provider_metadata: dict[str, Any] | None = None
    finish_reason: str | None = None
    limits = _limits.get()
    provider = getattr(model.provider, "name", None)
    loop = asyncio.get_running_loop()
    # Two clocks share one deadline. From the request being sent to the first
    # response body byte it is FIRST_BYTE_TIMEOUT (the prefill runs here);
    # from that byte on, STREAM_IDLE_TIMEOUT, moved forward on every byte and
    # every event. The SDK's own setup before the request (its first request
    # in a process imports the SDK) is on neither. For a provider whose
    # requests js does not see, the first clock starts at once and the first
    # event is the first byte.
    sees_request = _http_client(model) is not None
    deadline = asyncio.timeout(None)
    waiting = stream_transport.FirstByteWait()
    first_byte = False

    def sent() -> None:
        if first_byte:
            return
        if limits.first_byte is not None and not deadline.expired():
            deadline.reschedule(loop.time() + limits.first_byte)
        waiting.start()

    def alive() -> None:
        nonlocal first_byte
        if not first_byte:
            first_byte = True
            waiting.end()
        if deadline.expired():
            return
        deadline.reschedule(None if limits.idle is None else loop.time() + limits.idle)

    try:
        async with deadline:
            if not sees_request:
                sent()
            async with _open_stream(**kwargs, on_request=sent, on_bytes=alive,
                                    connect_timeout=limits.connect) as stream:
                async for event in stream:
                    # A transport js sees marks the first byte itself; an
                    # event before it (a stream start on the headers) is not one.
                    if first_byte or not sees_request or isinstance(event, _OUTPUT_EVENTS):
                        alive()
                    event_metadata = getattr(event, "provider_metadata", None)
                    if isinstance(event_metadata, dict):
                        stream_provider_metadata = event_metadata
                    if isinstance(event, ai.events.TextDelta):
                        if first_token_s is None:
                            first_token_s = time.perf_counter() - start
                        on_text(event.chunk)
                    elif isinstance(event, ai.events.ReasoningDelta) and on_reasoning is not None:
                        on_reasoning(event.chunk)
                    elif isinstance(event, ai.events.StreamEnd):
                        finish_reason = event.finish_reason
    except Exception as exc:
        # The expired deadline cancelled the read; asyncio.timeout reports that
        # as TimeoutError, and an SDK may have wrapped it on the way out.
        if deadline.expired():
            if first_byte:
                raise provider_errors.StreamIdleError(limits.idle, provider=provider) from exc
            raise provider_errors.FirstByteTimeoutError(limits.first_byte, provider=provider) from exc
        failure = provider_errors.request_failure(exc, provider=provider, limits=limits)
        if failure is exc:
            raise
        raise failure from exc
    finally:
        waiting.end()
    elapsed_s = time.perf_counter() - start

    text = stream.text
    reasoning = stream.message.reasoning
    usage = _usage_from_stream(stream)
    provider_metadata = (
        getattr(stream.message, "provider_metadata", None)
        or stream_provider_metadata
        or getattr(stream, "provider_metadata", None)
    )
    incomplete_reason = provider_errors.incomplete_reason_from_metadata(provider_metadata)
    if incomplete_reason is None and finish_reason == provider_errors.OUTPUT_CAP_FINISH:
        incomplete_reason = "max_output_tokens"
    if incomplete_reason is None and finish_reason == "content_filter":
        incomplete_reason = "content_filter"
    tool_calls = [
        ModelToolCall(
            id=part.tool_call_id,
            name=part.tool_name,
            arguments=part.tool_args,
        )
        for part in stream.message.tool_calls
    ]
    finish = provider_errors.incomplete_finish_reason(incomplete_reason) if incomplete_reason else ("tool_calls" if tool_calls else "stop")
    return ModelStreamResult(
        text=text,
        tool_calls=tool_calls,
        reasoning=reasoning,
        usage=usage,
        finish_reason=finish,
        assistant_message=stream.message,
        first_token_s=first_token_s,
        elapsed_s=elapsed_s,
        provider_metadata=provider_metadata if isinstance(provider_metadata, dict) else None,
        incomplete_reason=incomplete_reason,
    )


async def stream_model_async(
    *,
    model_id: str,
    provider_id: str | None,
    provider_base_url: str | None,
    provider_api_key: str | None,
    messages: list[ai.messages.Message],
    tools: list[ai.types.tools.Tool] | None,
    max_output_tokens: int | None,
    reasoning_effort: str | None,
    on_text: Callable[[str], None],
    on_reasoning: Callable[[str], None] | None = None,
    thinking_budget: int | None = None,
    provider_headers: dict[str, str] | None = None,
    provider_extra: dict[str, Any] | None = None,
    sampling: Sampling | None = None,
    trace_request: bool = False,
    trace_sink: Any = None,
    trace_request_schemas: bool = True,
    trace_request_from: int = 0,
    cache_key: str | None = None,
    stream_limits: retry.StreamLimits | None = None,
    settings: dict | None = None,
) -> ModelStreamResult:
    """Async entry point: stream one model turn on the CALLER'S event loop.

    This is the real primitive. It builds the model, streams ``ai.stream``, and
    closes the provider-owned client in ``finally`` — all without owning a loop,
    so many turns/subagents can run concurrently on one shared loop. The sync
    ``stream_model`` below wraps this for callers not yet on the async runtime.

    ``stream_limits`` are the request's clocks (`retry.StreamLimits`; None:
    none): CONNECT_TIMEOUT, FIRST_BYTE_TIMEOUT and STREAM_IDLE_TIMEOUT.
    """
    if (providers.normalize_provider_id(provider_id) or "") in request_params.OPENCODE_GO_PROVIDERS:
        provider_headers = {
            request_params.OPENCODE_SESSION_HEADER: cache_key or f"js-{uuid.uuid4().hex}",
            **(provider_headers or {}),
        }
    try:
        factory = _model_factory.get()
        if factory is None:
            factory = resolve_model
        model = factory(
            model_id,
            provider_id=provider_id,
            provider_base_url=provider_base_url,
            provider_api_key=provider_api_key,
            provider_headers=provider_headers,
        )
    except routing.ProviderNotLoggedInError:
        raise
    except Exception as exc:
        friendly = provider_errors.friendly_provider_error(exc, provider_id=provider_id)
        if friendly is not None:
            raise friendly from exc
        raise
    # The provider owns an HTTP client from here on; every exit below, including
    # a failure while the request is still being assembled, must close it.
    try:
        return await _stream_with_model(
            model,
            model_id=model_id,
            provider_id=provider_id,
            provider_base_url=provider_base_url,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            on_text=on_text,
            on_reasoning=on_reasoning,
            trace_request=trace_request,
            trace_sink=trace_sink,
            trace_request_schemas=trace_request_schemas,
            trace_request_from=trace_request_from,
            sampling=sampling,
            provider_extra=provider_extra,
            thinking_budget=thinking_budget,
            cache_key=cache_key,
            stream_limits=stream_limits,
            settings=settings,
        )
    finally:
        try:
            await model.provider.aclose()
        except Exception:
            pass


async def _stream_with_model(
    model: ai.Model,
    *,
    model_id: str,
    provider_id: str | None,
    provider_base_url: str | None,
    messages: list[ai.messages.Message],
    tools: list[ai.types.tools.Tool] | None,
    max_output_tokens: int | None,
    reasoning_effort: str | None,
    on_text: Callable[[str], None],
    on_reasoning: Callable[[str], None] | None,
    trace_request: bool,
    trace_sink: Any,
    trace_request_schemas: bool,
    trace_request_from: int,
    sampling: Sampling | None,
    provider_extra: dict[str, Any] | None,
    thinking_budget: int | None,
    cache_key: str | None,
    stream_limits: retry.StreamLimits | None,
    settings: dict | None,
) -> ModelStreamResult:
    wire = routing.resolve_wire(provider_id, base_url=provider_base_url, model_id=model_id)
    if wire.anthropic:
        messages = tool_result_messages.without_empty_text_parts(messages)
        systems = [message for message in messages if message.role == "system"]
        if len(systems) > 1:
            system = "\n\n".join("".join(part.text for part in message.parts
                                       if isinstance(part, ai.types.messages.TextPart))
                                   for message in systems)
            messages = [ai.system_message(system),
                        *(message for message in messages if message.role != "system")]

    params = request_params.build_request_params(
        model_id=model_id,
        provider_id=provider_id,
        provider_base_url=provider_base_url,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        thinking_budget=thinking_budget,
        provider_extra=provider_extra,
        sampling=sampling,
        cache_key=cache_key,
        settings=settings,
        wire=wire,
    )

    # DeepSeek, MiMo, and Anthropic-like providers are append-only in the sense
    # that we never rewrite prior assistant/tool messages to satisfy a transport.
    # History cleanup happens before send; the provider registry owns the policy.

    # The OpenAI chat-completions wire re-serializes replayed reasoning as a
    # non-standard ``message.reasoning`` field that some backends (glm) reject,
    # which otherwise breaks resume and mid-conversation model switches. Strip it
    # only for those models, so a stored session stays portable without rewriting
    # history for backends (mimo, kimi) that accept the field.
    if wire.chat_completions and reasoning.rejects_reasoning_replay(model_id.lower()):
        messages = tool_result_messages.strip_reasoning_parts(messages)

    if trace_request and trace_sink is not None:
        debug_trace.emit_request_trace(
            sink=trace_sink,
            model_id=model_id,
            provider_id=provider_id,
            provider_base_url=provider_base_url,
            params=params,
            messages=messages,
            tools=tools,
            dump_schemas=trace_request_schemas,
            dump_from=trace_request_from,
        )

    # The request goes out here: open its network-channel record, and let the
    # first reasoning or text chunk stand in for "connected" when the transport
    # cannot report its handshake.
    net_call = stream_transport.begin_call(
        str(getattr(model.provider, "base_url", None) or provider_base_url or provider_id or "ai-gateway"),
        "/".join(filter(None, (provider_id, model_id))),
    )
    net_token = stream_transport.set_call(net_call)
    if net_call is not None:
        _on_text, _on_reasoning = on_text, on_reasoning

        def on_text(chunk: str) -> None:
            net_call.first_token()
            _on_text(chunk)

        if _on_reasoning is not None:
            def on_reasoning(chunk: str) -> None:
                net_call.first_token()
                _on_reasoning(chunk)

    reasoning_token = _reasoning_sink.set(on_reasoning)
    limits_token = _limits.set(stream_limits or retry.StreamLimits())
    collected = stream_protocols.ReasoningBlocks()
    blocks_token = stream_protocols.reasoning_blocks_sink.set(collected)
    try:
        result = await _stream_async(
            model=model,
            messages=messages,
            tools=tools,
            params=params,
            on_text=on_text,
        )
        blocks = collected.blocks()
        if blocks is None:
            return result
        return replace(result, reasoning_blocks=blocks,
                       assistant_message=with_reasoning_blocks(result.assistant_message, blocks))
    except routing.ProviderNotLoggedInError:
        raise
    except Exception as exc:
        if net_call is not None:
            net_call.failed(exc)
        friendly = provider_errors.friendly_provider_error(exc, provider_id=provider_id)
        if friendly is not None:
            raise friendly from exc
        raise
    finally:
        stream_protocols.reasoning_blocks_sink.reset(blocks_token)
        _limits.reset(limits_token)
        _reasoning_sink.reset(reasoning_token)
        stream_transport.reset_call(net_token)


# The loop's own teardown protocol, not anybody's failure. `shutdown_asyncgens`
# throws GeneratorExit into every async generator still suspended at a yield; a
# generator whose teardown awaits (or whose teardown is itself another generator
# being closed in the same pass) cannot stop on that throw, and the close fails
# with one of these. Every one of them is raised BY the close protocol, after the
# work already succeeded.
_ASYNCGEN_SHUTDOWN_MESSAGE = "an error occurred during closing of asynchronous generator"
_ASYNCGEN_PROTOCOL_ERRORS = (
    "didn't stop after athrow()",
    "asynchronous generator is already running",
    "async generator is already running",
    "ignored GeneratorExit",
)


def install_asyncgen_shutdown_filter(loop: asyncio.AbstractEventLoop) -> None:
    """Arm the filter below on a loop js owns, immediately before shutting it down.

    Every sync entry point that runs a turn ends by closing its own loop, and
    every one of them hits the same upstream teardown, so each of them installs
    this. Arming it late keeps a running loop's own error reporting untouched.
    """
    loop.set_exception_handler(_asyncgen_shutdown_filter(loop.get_exception_handler()))


def _asyncgen_shutdown_filter(previous: Callable[..., None] | None) -> Callable[..., None]:
    """An asyncio exception handler that drops exactly the close-protocol noise.

    Nothing else: a teardown that fails for a real reason (an OSError closing a
    file, a ValueError out of a `finally`) still reaches the previous handler,
    and so does any other loop error.
    """

    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        # asyncio appends the generator's repr to the message, so match the stem.
        if (
            str(context.get("message", "")).startswith(_ASYNCGEN_SHUTDOWN_MESSAGE)
            and isinstance(exc, RuntimeError)
            and any(needle in str(exc) for needle in _ASYNCGEN_PROTOCOL_ERRORS)
        ):
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    return handler


def run_owning_loop(coro: Any) -> Any:
    """``asyncio.run(coro)``, minus one upstream traceback on the way out.

    A completed turn used to print a `RuntimeError` traceback after its own
    telemetry line, because `asyncio.run`'s last act is `shutdown_asyncgens()`
    and the transport leaves a response-body iterator suspended at a yield whose
    teardown awaits. We cannot close that iterator ourselves: it is not the
    object the SDK closes (`PoolByteStream.aclose()` closes `self._stream`, a
    different object), and it belongs to the transport, not to js. The defect is
    upstream, so this narrows what the loop reports rather than pretending to
    fix it — only during OUR shutdown of OUR loop, and only for errors the close
    protocol itself raised.
    """
    runner = asyncio.Runner()
    try:
        return runner.run(coro)
    finally:
        install_asyncgen_shutdown_filter(runner.get_loop())
        runner.close()


def stream_model(*, retry_budget: retry.Budget | None = None, **kwargs: Any) -> ModelStreamResult:
    """Sync wrapper over :func:`stream_model_async` — spins a throwaway loop per
    call. This is the OLD blocking path; the non-blocking runtime calls
    ``stream_model_async`` directly on its shared loop. Kept for callers
    off the loop.

    With ``retry_budget`` a provider_errors.retryable failure sends the request again under it;
    without one the first failure raises.
    """
    if retry_budget is None:
        return run_owning_loop(stream_model_async(**kwargs))
    return run_owning_loop(retry.call(lambda: stream_model_async(**kwargs), retry_budget))
