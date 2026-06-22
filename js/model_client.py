"""Single import boundary for the Vercel AI Python SDK (`ai`).

This is the only production module that imports `ai`. It adapts the SDK's
async, part-based API to the synchronous, dict-based runtime used by `js`.
This is the canonical provider boundary for the migration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

import ai

from . import codex_auth, codex_provider, providers
from .sampling import Sampling
import ai.types.messages
import ai.types.tools
import ai.types.usage
import ai.models


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
    finish_reason: str  # "tool_calls" when tool_calls is non-empty, otherwise "stop"
    assistant_message: ai.messages.Message


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
        return ai.get_model(model_id)

    provider_def = providers.get_provider(provider_id)
    if provider_def is not None and not provider_def.supports_model(model_id):
        allowed = ", ".join(provider_def.allowed_models)
        raise ValueError(f"{provider_def.id} does not serve model {model_id}; allowed models: {allowed}")
    canonical_id = providers.normalize_provider_id(provider_id) or provider_id
    if codex_auth.is_codex_provider(canonical_id):
        provider = codex_provider.provider_from_login_or_token(
            provider_base_url=provider_base_url,
            provider_api_key=provider_api_key,
        )
        return ai.Model(model_id, provider=provider)

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
    provider = ai.get_provider(
        sdk_provider_id,
        base_url=provider_base_url,
        api_key=provider_api_key,
        headers=headers or None,
    )
    return ai.Model(model_id, provider=provider)


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
        args = ai.types.tools.FunctionToolArgs(
            description=fn.get("description") or "",
            params=fn.get("parameters") or {"type": "object"},
        )
        tools.append(
            ai.types.tools.Tool(kind="function", name=name, args=args)
        )
    return tools


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
            out.append(ai.assistant_message(*parts))
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
    params: dict[str, Any] | None,
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

    async with ai.stream(**kwargs) as stream:
        async for event in stream:
            if isinstance(event, ai.events.TextDelta):
                on_text(event.chunk)

    text = stream.text
    reasoning = stream.message.reasoning
    usage = _usage_from_stream(stream)
    tool_calls = [
        ModelToolCall(
            id=part.tool_call_id,
            name=part.tool_name,
            arguments=part.tool_args,
        )
        for part in stream.message.tool_calls
    ]
    finish = "tool_calls" if tool_calls else "stop"
    return ModelStreamResult(
        text=text,
        tool_calls=tool_calls,
        reasoning=reasoning,
        usage=usage,
        finish_reason=finish,
        assistant_message=stream.message,
    )


def stream_model(
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
    executor: ai.models.StreamExecutor | None = None,
    sampling: Sampling | None = None,
) -> ModelStreamResult:
    """Synchronous entry point: stream one model turn and return the result.

    This function builds the model, runs ``ai.stream`` in a fresh event loop,
    and closes the provider-owned client in ``finally``.
    """
    model = resolve_model(
        model_id,
        provider_id=provider_id,
        provider_base_url=provider_base_url,
        provider_api_key=provider_api_key,
        provider_headers=provider_headers,
    )
    params: dict[str, Any] = {}
    # Per-provider params: this is the canonical place to encode quirks.
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
    if max_output_tokens is not None and not is_codex:
        params["max_tokens"] = max_output_tokens
    if reasoning_effort is not None and not is_minimax:
        if is_codex:
            params["reasoning_effort"] = reasoning_effort
        elif reasoning_effort == "none":
            if not explicit_provider:
                params["reasoning"] = {"effort": None}
        elif explicit_provider:
            if sdk_provider_name == "openai":
                params["reasoning_effort"] = reasoning_effort
        else:
            params["reasoning"] = {"effort": reasoning_effort}
    # DeepSeek gets the maximum reasoning budget by default. Direct providers
    # use OpenAI-compatible chat completions, so provider-specific extras must go
    # through extra_body instead of top-level kwargs.
    if is_deepseek and reasoning_effort != "none":
        if explicit_provider:
            extra_body = dict(params.get("extra_body") or {})
            extra_body.setdefault("max_reasoning_tokens", 32_000)
            params["extra_body"] = extra_body
        else:
            params.setdefault("max_reasoning_tokens", 32_000)

    sampling_params = (sampling or Sampling()).call_params(
        provider_def.transport if provider_def is not None else None
    )
    if sampling_params:
        sampling_extra_body = sampling_params.pop("extra_body", None)
        params.update(sampling_params)
        if sampling_extra_body:
            extra_body = dict(params.get("extra_body") or {})
            extra_body.update(sampling_extra_body)
            params["extra_body"] = extra_body

    # DeepSeek, MiMo, and Anthropic-like providers are append-only in the sense
    # that we never rewrite prior assistant/tool messages to satisfy a transport.
    # History cleanup happens before send; the provider registry owns the policy.

    try:
        return asyncio.run(
            _stream_async(
                model=model,
                messages=messages,
                tools=tools,
                params=params if params else None,
                executor=executor,
                on_text=on_text,
            )
        )
    finally:
        try:
            asyncio.run(model.provider.aclose())
        except Exception:
            pass
