"""Single import boundary for the Vercel AI Python SDK (`ai`).

This is the only production module that imports `ai`. It adapts the SDK's
async, part-based API to the synchronous, dict-based runtime used by `js`.
This is the canonical provider boundary for the migration.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

import ai

from . import codex_auth, codex_provider, providers, reasoning, routing
from .sampling import Sampling
import ai.types.messages
import ai.types.tools
import ai.types.usage
import ai.models
from ai.models.core import params as ai_params


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
    # reasoning at end (not via on_text), and a tool-only turn streams no text,
    # so it is None when the model emitted no visible text. elapsed_s is the full
    # stream duration. Both default to 0/None so older constructors stay valid.
    first_token_s: float | None = None
    elapsed_s: float = 0.0
    provider_metadata: dict[str, Any] | None = None
    incomplete_reason: str | None = None


class FriendlyProviderError(routing.ProviderNotLoggedInError):
    """Provider-boundary failure already formatted for one-line user display."""


def _provider_label(provider_id: str | None, exc: BaseException | None = None) -> str:
    provider = getattr(exc, "provider", None) if exc is not None else None
    if isinstance(provider, str) and provider:
        return provider
    return provider_id or "default"


def _detail(exc: BaseException, *, max_len: int = 220) -> str:
    text = " ".join(str(exc).split())
    if not text:
        return exc.__class__.__name__
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _friendly_provider_error(
    exc: BaseException,
    *,
    provider_id: str | None,
) -> FriendlyProviderError | None:
    provider = _provider_label(provider_id, exc)
    detail = _detail(exc)

    if isinstance(exc, TypeError) and "Could not resolve authentication method" in str(exc):
        return FriendlyProviderError(
            f"provider {provider!r} needs an API key; run `js --login {provider}` "
            "or `set provider.api_key <value>`; detail: SDK could not resolve authentication"
        )

    if isinstance(exc, ValueError) and str(exc).startswith("unknown provider id:"):
        unknown = str(exc).partition(":")[2].strip()
        if unknown.startswith(("'", '"')) and len(unknown) >= 2:
            provider = unknown[1:-1]
        return FriendlyProviderError(
            f"unknown provider {provider!r}; run `js --login {provider}` "
            "(js --list-models shows what's runnable)"
        )

    if isinstance(exc, ai.ProviderAuthenticationError):
        return FriendlyProviderError(
            f"provider {provider!r} authentication failed; detail: {detail}; "
            f"run `js --login {provider}` or `set provider.api_key <value>`"
        )

    if isinstance(exc, ai.ProviderNotConfiguredError):
        return FriendlyProviderError(
            f"provider {provider!r} is not configured; detail: {detail}; "
            f"run `js --login {provider}` or `set provider.api_key <value>`"
        )

    if isinstance(exc, ai.ProviderConnectionError):
        return FriendlyProviderError(
            f"provider {provider!r} connection failed; detail: {detail}; "
            "`set provider.base_url <url>` or check the endpoint"
        )

    if isinstance(exc, ai.ProviderStatusError):
        return FriendlyProviderError(
            f"provider {provider!r} request failed; detail: {detail}; "
            f"run `js --login {provider}`, `set provider.api_key <value>`, "
            "or `set provider.base_url <url>`"
        )

    if isinstance(exc, ai.ProviderAPIError):
        return FriendlyProviderError(
            f"provider {provider!r} request failed; detail: {detail}; "
            f"run `js --login {provider}` or check `set provider.base_url <url>`"
        )

    return None


def incomplete_reason_from_metadata(provider_metadata: Any) -> str | None:
    """Return a stable incomplete reason from provider metadata, if present.

    Codex streams surface ``response.incomplete`` as provider metadata on the
    assistant message / stream end. Keep this helper permissive so provider
    versions that expose either ``incomplete_reason`` or an OpenAI-shaped
    ``incomplete_details.reason`` do not make a truncated turn look like a clean
    stop again. Unknown-but-flagged incomplete streams get an explicit
    ``unknown`` reason rather than disappearing.
    """
    if not isinstance(provider_metadata, dict):
        return None
    reason = provider_metadata.get("incomplete_reason")
    details = provider_metadata.get("incomplete_details")
    if reason is None and isinstance(details, dict):
        reason = details.get("reason")
    if provider_metadata.get("incomplete") or reason is not None:
        return str(reason or "unknown")
    return None


def incomplete_finish_reason(reason: str) -> str:
    return f"incomplete:{reason}"


def resolve_model(
    model_id: str,
    *,
    provider_id: str | None,
    provider_base_url: str | None,
    provider_api_key: str | None,
    provider_headers: dict[str, str] | None = None,
) -> ai.Model:
    """Return an `ai.Model`, optionally bound to an explicit js provider.

    ``provider_id`` is a user-facing js id.  The provider registry translates it
    to the SDK/API shape while the model id is passed through verbatim.
    """
    if provider_id is None:
        # No js provider resolved. An explicit base URL is a deliberate endpoint
        # choice, so honor it (the SDK-default gateway path). With neither a
        # provider nor a base URL, riding the SDK's own env keys (AI_GATEWAY_API_KEY,
        # OPENAI_API_KEY, ...) is exactly the env-farming the login gate forbids:
        # fail with the same actionable not-logged-in message instead.
        if provider_base_url:
            return ai.get_model(model_id)
        raise routing.ProviderNotLoggedInError(routing.unconfigured_model_message(model_id))

    provider_def = providers.get_provider(provider_id)
    # No client-side allowlist gate: ``allowed_models`` is a curated hint used to
    # filter noisy /models listings, NOT an authority on what the endpoint serves.
    # It goes stale the moment a provider ships a new id (e.g. opencode-go's
    # glm-5.2), so refusing here only blocks a model the server happily answers.
    # Let the request through; the provider is the one source of truth and will
    # 400 with its own message if the id is genuinely unknown.
    canonical_id = providers.normalize_provider_id(provider_id) or provider_id
    if codex_auth.is_codex_provider(canonical_id):
        provider = codex_provider.provider_from_login_or_token(
            provider_base_url=provider_base_url,
            provider_api_key=provider_api_key,
        )
        return ai.Model(id=model_id, provider=provider)

    if provider_def is not None:
        providers.assert_endpoint_configured(provider_def, provider_base_url)

    sdk_provider_id = (
        provider_def.effective_sdk_provider_id
        if provider_def is not None and provider_def.effective_sdk_provider_id
        else canonical_id
    )
    headers = dict(provider_headers or {})
    if provider_def is not None and provider_def.headers:
        headers = {**provider_def.headers, **headers}
    # ai>=0.2.1 flipped the openai SDK's default wire to the Responses API; every
    # chat-completions endpoint js targets through the openai SDK (opencode-go,
    # mimo, ollama, llama.cpp, custom OpenAI-compatible bases) 404s on /responses.
    # Pin chat-completions for those; only the explicit Responses transport keeps
    # the new default.
    protocol = None
    transport = provider_def.transport if provider_def is not None else None
    if sdk_provider_id == "openai" and transport != "custom_responses":
        from ai.providers.openai.protocol import OpenAIChatCompletionsProtocol

        protocol = OpenAIChatCompletionsProtocol()
    provider = ai.get_provider(
        sdk_provider_id,
        base_url=provider_base_url,
        api_key=provider_api_key,
        headers=headers or None,
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


def _strip_reasoning_parts(messages: list[ai.messages.Message]) -> list[ai.messages.Message]:
    """Drop reasoning parts from assistant messages.

    The OpenAI chat-completions protocol re-serializes any ``ReasoningPart`` in
    the history as a non-standard ``message.reasoning`` field. Some gateways
    (opencode-go) reject it outright ("Extra inputs are not permitted, field:
    messages[..].reasoning"), so a stored session can't be replayed there or
    switched onto mid-conversation. Replayed chain-of-thought has no value on
    this wire anyway — the model re-reasons — so strip it. Providers that do
    want reasoning replayed (DeepSeek's own provider requires ``reasoning_content``;
    Anthropic keeps thinking blocks) use their own protocols and never hit this.
    """
    out: list[ai.messages.Message] = []
    for msg in messages:
        if msg.role != "assistant" or not any(
            isinstance(part, ai.types.messages.ReasoningPart) for part in msg.parts
        ):
            out.append(msg)
            continue
        kept = [part for part in msg.parts if not isinstance(part, ai.types.messages.ReasoningPart)]
        if kept:
            out.append(ai.assistant_message(*kept))
    return out


def _is_message_part(value: Any) -> bool:
    return hasattr(value, "kind")


def _coerce_parts(content: Any) -> list[Any]:
    """Build ``ai`` message parts from string content or mixed values."""
    if isinstance(content, str):
        return [ai.types.messages.TextPart(text=content)]
    if isinstance(content, (list, tuple)):
        parts: list[Any] = []
        for item in content:
            if isinstance(item, str):
                parts.append(ai.types.messages.TextPart(text=item))
            elif _is_message_part(item):
                parts.append(item)
            else:
                raise ValueError(f"unsupported message content item: {item!r}")
        return parts
    if _is_message_part(content):
        return [content]
    raise ValueError(f"unsupported message content: {content!r}")


def history_to_ai_messages(
    system: str, messages: list[dict]
) -> list[ai.messages.Message]:
    """Convert the harness JSONL history into ``ai.messages.Message`` objects.

    The harness history uses OpenAI-shaped dicts. We translate them at the
    provider boundary so the runtime itself keeps the stable JSONL schema.

    * ``role == "system"`` -> ``ai.system_message(system)``
    * ``role == "user"`` -> ``ai.user_message(content)``
    * ``role == "assistant"`` -> ``ai.assistant_message`` with text,
      reasoning, and tool call parts.
    * ``role == "tool"`` -> ``ai.tool_message`` with a ``ToolResultPart``.
    """
    out: list[ai.messages.Message] = []
    if system:
        out.append(ai.system_message(system))

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            # History system entries are rare; treat like explicit system text.
            out.append(ai.system_message(str(msg.get("content", ""))))
            continue
        if role == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                out.append(ai.user_message(content))
            else:
                out.append(ai.user_message(*_coerce_parts(content)))
            continue
        if role == "assistant":
            parts: list[Any] = []
            reasoning = msg.get("reasoning_content")
            if reasoning and msg.get("tool_calls"):
                parts.append(ai.thinking(str(reasoning)))
            content = msg.get("content")
            if content:
                parts.extend(_coerce_parts(content))
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                parts.append(
                    ai.types.messages.ToolCallPart(
                        tool_call_id=tc.get("id", ""),
                        tool_name=fn.get("name", ""),
                        tool_args=fn.get("arguments", ""),
                    )
                )
            assistant = ai.assistant_message(*parts)
            provider_metadata = msg.get("provider_metadata")
            if isinstance(provider_metadata, dict):
                assistant = assistant.model_copy(update={"provider_metadata": provider_metadata})
            out.append(assistant)
            continue
        if role == "tool":
            content = msg.get("content")
            is_error = isinstance(content, str) and content.startswith("ERROR")
            out.append(
                ai.tool_message(
                    tool_call_id=msg.get("tool_call_id", ""),
                    tool_name=msg.get("name", ""),
                    result=content,
                    is_error=is_error,
                )
            )
            continue
        raise ValueError(f"unsupported history role: {role!r}")

    return out


def _image_result_payload(result: str) -> tuple[str, str, bytes] | str | None:
    prefix = "IMAGE_RESULT\t"
    if not isinstance(result, str) or not result.startswith(prefix):
        return None
    parts = result.split("\t", 3)
    if len(parts) != 4:
        return "ERROR: malformed image result marker"
    _, raw_path, mime, stub = parts
    try:
        data = Path(raw_path).read_bytes()
    except OSError as exc:
        return f"ERROR: could not read visual result {raw_path}: {exc}"
    return stub, mime, data


def _build_tool_result_part(
    tool_call_id: str, tool_name: str, result: str
) -> ai.types.messages.ToolResultPart:
    """Create the text/stub ``ToolResultPart`` for a tool result."""
    payload = _image_result_payload(result)
    if payload is None:
        return ai.tool_result_part(
            tool_call_id,
            result=result,
            tool_name=tool_name,
            is_error=isinstance(result, str) and result.startswith("ERROR"),
        )
    if isinstance(payload, str):
        return ai.tool_result_part(
            tool_call_id,
            result=payload,
            tool_name=tool_name,
            is_error=True,
        )
    stub, _mime, _data = payload
    return ai.tool_result_part(
        tool_call_id,
        result=stub,
        tool_name=tool_name,
        is_error=False,
    )


def build_tool_result_message(
    tool_call_id: str, tool_name: str, result: Any
) -> ai.messages.Message:
    """Provider-facing tool result message for a single completed tool call."""
    if isinstance(result, ai.messages.Message):
        return result
    if hasattr(result, "kind"):
        part = result
    else:
        part = _build_tool_result_part(tool_call_id, tool_name, str(result))
    return ai.tool_message(part)


def build_tool_result_messages(
    tool_call_id: str, tool_name: str, result: Any
) -> list[ai.messages.Message]:
    """Provider-facing messages for a completed tool call.

    Image markers must be visible as a normal user message with a ``FilePart``;
    OpenAI/Anthropic tool-result converters stringify ``ToolResultPart.result``.
    Persisted history still receives only the dehydrated text stub.
    """
    if not isinstance(result, str):
        return [build_tool_result_message(tool_call_id, tool_name, result)]
    payload = _image_result_payload(result)
    if payload is None:
        return [build_tool_result_message(tool_call_id, tool_name, result)]
    if isinstance(payload, str):
        return [
            ai.tool_message(
                ai.tool_result_part(
                    tool_call_id,
                    result=payload,
                    tool_name=tool_name,
                    is_error=True,
                )
            )
        ]
    stub, mime, data = payload
    return [
        ai.tool_message(
            ai.tool_result_part(
                tool_call_id,
                result=stub,
                tool_name=tool_name,
                is_error=False,
            )
        ),
        ai.user_message(
            ai.types.messages.TextPart(text=stub),
            ai.types.messages.FilePart(data=data, media_type=mime),
        ),
    ]


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


async def _stream_async(
    model: ai.Model,
    messages: list[ai.messages.Message],
    tools: list[ai.types.tools.Tool] | None,
    params: ai_params.InferenceRequestParams | None,
    executor: ai.models.StreamExecutor | None,
    on_text: Callable[[str], None],
) -> ModelStreamResult:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
    }
    if params is not None:
        kwargs["params"] = params
    if executor is not None:
        kwargs["executor"] = executor

    start = time.perf_counter()
    first_token_s: float | None = None
    stream_provider_metadata: dict[str, Any] | None = None
    async with ai.stream(**kwargs) as stream:
        async for event in stream:
            event_metadata = getattr(event, "provider_metadata", None)
            if isinstance(event_metadata, dict):
                stream_provider_metadata = event_metadata
            if isinstance(event, ai.events.TextDelta):
                if first_token_s is None:
                    first_token_s = time.perf_counter() - start
                on_text(event.chunk)
    elapsed_s = time.perf_counter() - start

    text = stream.text
    reasoning = stream.message.reasoning
    usage = _usage_from_stream(stream)
    provider_metadata = (
        getattr(stream.message, "provider_metadata", None)
        or stream_provider_metadata
        or getattr(stream, "provider_metadata", None)
    )
    incomplete_reason = incomplete_reason_from_metadata(provider_metadata)
    tool_calls = [
        ModelToolCall(
            id=part.tool_call_id,
            name=part.tool_name,
            arguments=part.tool_args,
        )
        for part in stream.message.tool_calls
    ]
    finish = incomplete_finish_reason(incomplete_reason) if incomplete_reason else ("tool_calls" if tool_calls else "stop")
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


def _sampler_map(values: dict) -> dict:
    """Encode the TOP-LEVEL sampling knobs this transport accepts as ai>=0.2.1
    SamplerParamsMap entries (keyed by param class). Knobs ``call_params`` routed
    into ``extra_body`` (top_k/repetition_penalty on the openai-compatible family)
    stay OUT of here: the OpenAI protocol hard-raises on TopK/RepetitionPenalty
    param classes, so those ride through as raw extra_body instead."""
    out: dict = {}
    if "temperature" in values:
        out[ai_params.TemperatureSamplerParams] = ai_params.TemperatureSamplerParams(
            temperature=values["temperature"]
        )
    if "top_p" in values:
        out[ai_params.TopPSamplerParams] = ai_params.TopPSamplerParams(top_p=values["top_p"])
    if "top_k" in values:
        out[ai_params.TopKSamplerParams] = ai_params.TopKSamplerParams(top_k=values["top_k"])
    rep: dict[str, float] = {}
    if "repetition_penalty" in values:
        rep["repetition_penalty"] = values["repetition_penalty"]
    if "presence_penalty" in values:
        rep["presence_penalty"] = values["presence_penalty"]
    if rep:
        out[ai_params.RepetitionPenaltyParams] = ai_params.RepetitionPenaltyParams(**rep)
    return out


def _build_inference_params(
    sampling: Sampling,
    transport: str | None,
    *,
    reasoning: ai_params.ReasoningParams | None,
    output: ai_params.OutputParams | None,
    extra_body: dict[str, Any],
) -> ai_params.InferenceRequestParams | None:
    """Assemble an ``InferenceRequestParams`` from the parts, or None when there
    is nothing to send (so the provider keeps every default)."""
    kwargs: dict[str, Any] = {}
    values = dict(sampling.call_params(transport))
    sampler_extra = values.pop("extra_body", {}) or {}
    sampler_map = _sampler_map(values)
    if sampler_map:
        kwargs["sampling"] = sampler_map
    if reasoning is not None:
        kwargs["reasoning"] = reasoning
    if output is not None:
        kwargs["output"] = output
    merged_extra = {**sampler_extra, **(extra_body or {})}
    if merged_extra:
        kwargs["extra_body"] = merged_extra
    if not kwargs:
        return None
    return ai_params.InferenceRequestParams(**kwargs)


def _debug_json(value: Any) -> str:
    import json as _json

    return _json.dumps(value, indent=2, default=str, ensure_ascii=False)


def _message_to_debug(msg: ai.messages.Message) -> Any:
    """Best-effort full serialization of one outgoing message. Never raises."""
    try:
        return msg.model_dump(mode="json")
    except Exception:
        parts = []
        for part in getattr(msg, "parts", None) or []:
            parts.append(
                {k: getattr(part, k) for k in ("text", "tool_name", "tool_args", "tool_call_id")
                 if hasattr(part, k)}
            )
        return {"role": getattr(msg, "role", "?"), "parts": parts}


def _tool_to_debug(tool: ai.types.tools.Tool) -> dict:
    spec = getattr(tool, "spec", None)
    return {
        "name": getattr(tool, "name", "?"),
        "description": getattr(spec, "description", ""),
        "parameters": getattr(spec, "params", {}),
    }


def _emit_request_trace(
    *,
    sink: Any,
    model_id: str,
    provider_id: str | None,
    provider_base_url: str | None,
    params: ai_params.InferenceRequestParams | None,
    messages: list[ai.messages.Message],
    tools: list[ai.types.tools.Tool] | None,
    dump_schemas: bool,
    dump_from: int,
) -> None:
    """Byte-honest dump of the request as it leaves js for the SDK: the resolved
    provider/model/params, and — once per turn — the unclipped system prompt and
    FULL tool spec JSON (descriptions and all), then the messages newly sent this
    call. This is the instrument the tool-contract audit reads, so it must not
    clip or summarize. Never raises: a debug trace may not break a turn.

    Written ONLY to ``sink`` (the autolog file / --debug-file), never to stdout —
    this dump is far too large for the terminal. If ``sink`` is None it is a no-op.

    Note: for custom providers (codex) this shows the request js hands the SDK,
    including the resolved base_url — the provider's own in-SDK reshaping to its
    wire format happens downstream and is not captured here.
    """
    if sink is None:
        return
    try:
        header = {
            "model_id": model_id,
            "provider_id": provider_id,
            "base_url": provider_base_url or "provider-default",
            "message_count": len(messages),
            "tool_count": len(tools) if tools else 0,
        }
        if params is not None:
            try:
                header["params"] = params.model_dump(mode="json")
            except Exception:
                header["params"] = str(params)
        blocks = ["\n━━ REQUEST (model_client) ━━", _debug_json(header)]
        if dump_schemas:
            sys_txt = ""
            if messages and getattr(messages[0], "role", None) == "system":
                first_parts = getattr(messages[0], "parts", None) or []
                sys_txt = getattr(first_parts[0], "text", "") if first_parts else ""
            blocks.append("── SYSTEM PROMPT (unclipped) ──")
            blocks.append(sys_txt)
            blocks.append(f"── TOOL SCHEMAS ({len(tools) if tools else 0}) ──")
            blocks.append(_debug_json([_tool_to_debug(t) for t in (tools or [])]))
        new_msgs = messages[dump_from:] if dump_from else messages
        blocks.append(f"── MESSAGES (+{len(new_msgs)}) ──")
        blocks.append(_debug_json([_message_to_debug(m) for m in new_msgs]))
        sink.write("\n".join(blocks) + "\n")
    except Exception:
        pass


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
    provider_headers: dict[str, str] | None = None,
    provider_extra: dict[str, Any] | None = None,
    executor: ai.models.StreamExecutor | None = None,
    sampling: Sampling | None = None,
    trace_request: bool = False,
    trace_sink: Any = None,
    trace_request_schemas: bool = True,
    trace_request_from: int = 0,
) -> ModelStreamResult:
    """Async entry point: stream one model turn on the CALLER'S event loop.

    This is the real primitive. It builds the model, streams ``ai.stream``, and
    closes the provider-owned client in ``finally`` — all without owning a loop,
    so many turns/subagents can run concurrently on one shared loop. The sync
    ``stream_model`` below wraps this for callers not yet on the async runtime.
    """
    try:
        model = resolve_model(
            model_id,
            provider_id=provider_id,
            provider_base_url=provider_base_url,
            provider_api_key=provider_api_key,
            provider_headers=provider_headers,
        )
    except routing.ProviderNotLoggedInError:
        raise
    except Exception as exc:
        friendly = _friendly_provider_error(exc, provider_id=provider_id)
        if friendly is not None:
            raise friendly from exc
        raise
    # Per-provider params: this is the canonical place to encode quirks. We build
    # a structured ``InferenceRequestParams`` (ai>=0.2.1); the SDK translates each
    # field to the provider's wire shape, so js no longer hand-routes sampling
    # knobs into top-level-vs-extra_body per transport — it declares intent and
    # lets the provider drop what it can't take.
    # provider.extra is free-form raw passthrough. In the structured model every
    # raw kwarg rides extra_body, so fold a nested ``extra_body`` key up and carry
    # any sibling keys alongside it.
    extra_body: dict[str, Any] = {}
    _provider_extra = dict(provider_extra or {})
    _nested_extra_body = _provider_extra.pop("extra_body", None)
    if isinstance(_nested_extra_body, dict):
        extra_body.update(_nested_extra_body)
    extra_body.update(_provider_extra)
    provider_def = providers.get_provider(provider_id)
    provider_name = (provider_def.id if provider_def is not None else (provider_id or "")).lower()
    sdk_provider_name = (
        provider_def.effective_sdk_provider_id
        if provider_def is not None and provider_def.effective_sdk_provider_id
        else (provider_id or "")
    ).lower()
    model_name = model_id.lower()
    explicit_provider = provider_id is not None
    is_codex = codex_auth.is_codex_provider(provider_name)
    is_deepseek = provider_name == "deepseek" or "deepseek" in model_name
    is_minimax = provider_name.startswith("minimax") or model_name.startswith("minimax") or "minimax" in model_name

    output_params = (
        ai_params.OutputParams(max_tokens=max_output_tokens)
        if max_output_tokens is not None and not is_codex
        else None
    )

    reasoning_params: ai_params.ReasoningParams | None = None
    if reasoning_effort is not None and not is_minimax:
        # Direct DeepSeek/Anthropic-style providers steer reasoning via their own
        # budget (below) / thinking flag, not the OpenAI effort knob. Codex and
        # any openai-SDK endpoint take the effort knob — snapped to the stops the
        # target actually serves (js/reasoning.py), so the one dial fits each.
        steers_via_effort = is_codex or sdk_provider_name == "openai" or not explicit_provider
        if steers_via_effort:
            if is_codex:
                allowed: frozenset[str] | None = reasoning.CODEX_EFFORTS
            elif explicit_provider and sdk_provider_name == "openai":
                allowed = reasoning.supported_efforts(model_name)
            else:
                allowed = None  # gateway / implicit: let it self-normalize
            effort = reasoning.snap_effort(reasoning_effort, allowed)
            if effort == "none":
                # Disable explicitly only where "none" is a real stop (glm) or on
                # the implicit/gateway path that historically disabled; for an
                # unknown explicit model, omit the knob and take its default
                # rather than send a non-standard reasoning_effort="none".
                if not explicit_provider or (allowed is not None and "none" in allowed):
                    reasoning_params = ai_params.ReasoningParams(effort=None)
            elif effort is not None:
                reasoning_params = ai_params.ReasoningParams(effort=effort)

    # DeepSeek gets the maximum reasoning budget by default as an extra_body
    # field. `is_deepseek` matches on provider name OR a "deepseek" substring in
    # the model id, so this also rides the implicit gateway path (provider_id
    # None, e.g. `deepseek/deepseek-v3`), not only DeepSeek's own OpenAI-compatible
    # endpoint — the budget is a harmless passthrough that other gateways forward
    # or ignore, and both routes are exercised in daily use.
    if is_deepseek and reasoning_effort != "none":
        extra_body.setdefault("max_reasoning_tokens", 32_000)

    params = _build_inference_params(
        sampling or Sampling(),
        provider_def.transport if provider_def is not None else None,
        reasoning=reasoning_params,
        output=output_params,
        extra_body=extra_body,
    )

    # DeepSeek, MiMo, and Anthropic-like providers are append-only in the sense
    # that we never rewrite prior assistant/tool messages to satisfy a transport.
    # History cleanup happens before send; the provider registry owns the policy.

    # The OpenAI chat-completions wire re-serializes replayed reasoning as a
    # non-standard ``message.reasoning`` field that some backends (glm) reject,
    # which otherwise breaks resume and mid-conversation model switches. Strip it
    # only for those models, so a stored session stays portable without rewriting
    # history for backends (mimo, kimi) that accept the field.
    transport = provider_def.transport if provider_def is not None else None
    if (
        sdk_provider_name == "openai"
        and transport != "custom_responses"
        and reasoning.rejects_reasoning_replay(model_name)
    ):
        messages = _strip_reasoning_parts(messages)

    if trace_request and trace_sink is not None:
        _emit_request_trace(
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

    try:
        return await _stream_async(
            model=model,
            messages=messages,
            tools=tools,
            params=params,
            executor=executor,
            on_text=on_text,
        )
    except routing.ProviderNotLoggedInError:
        raise
    except Exception as exc:
        friendly = _friendly_provider_error(exc, provider_id=provider_id)
        if friendly is not None:
            raise friendly from exc
        raise
    finally:
        try:
            await model.provider.aclose()
        except Exception:
            pass


def stream_model(**kwargs: Any) -> ModelStreamResult:
    """Sync wrapper over :func:`stream_model_async` — spins a throwaway loop per
    call. This is the OLD blocking path; the non-blocking runtime calls
    ``stream_model_async`` directly on its shared loop. Kept so un-migrated
    callers (and the current sync run_turn) keep working during the transition.
    """
    return asyncio.run(stream_model_async(**kwargs))
