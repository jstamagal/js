"""The wire protocols a model's stream is read through: reasoning blocks kept as they arrive, Anthropic thinking and redacted thinking, reasoning_content, and a Responses stream checked as it goes."""

from __future__ import annotations

import contextvars
import uuid
from contextlib import aclosing, asynccontextmanager
from types import SimpleNamespace
from typing import Any, Literal

import ai
import ai.types.messages
from ai.providers import history_utils
from ai.providers.anthropic.protocol import AnthropicMessagesProtocol
from ai.providers.openai.protocol import OpenAIChatCompletionsProtocol, OpenAIResponsesProtocol

from . import messages as msgs
from .history_record import JS_METADATA_KEY, valid_reasoning_blocks

# The fields of a reasoning block a stream sends in fragments.
_REASONING_BLOCK_STRINGS = ("thinking", "signature", "data")


def _field(obj: Any, name: str) -> Any:
    """``obj[name]`` for a dict, else the attribute or the pydantic extra."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(name)
    return value


class ReasoningBlocks:
    """Collects `reasoning_blocks` from a chat-completions reply: fragments
    in stream deltas joined by index like tool-call deltas, or the whole list
    on a non-streaming message."""

    def __init__(self) -> None:
        self._parts: dict[int, dict[str, Any]] = {}
        self._whole: list[dict[str, Any]] | None = None

    def observe(self, chunk: Any) -> None:
        choices = _field(chunk, "choices")
        if not choices:
            return
        choice = choices[0]
        for fragment in _field(_field(choice, "delta"), "reasoning_blocks") or ():
            self._add(fragment)
        whole = _field(_field(choice, "message"), "reasoning_blocks")
        if isinstance(whole, list):
            self._whole = [{k: v for k, v in dict(b).items() if k != "index"} for b in whole if isinstance(b, dict)]

    def _add(self, fragment: Any) -> None:
        if not isinstance(fragment, dict):
            return
        index = fragment.get("index", 0)
        block = self._parts.setdefault(index if isinstance(index, int) else 0, {})
        for key, value in fragment.items():
            if key == "index":
                continue
            if key in _REASONING_BLOCK_STRINGS and isinstance(value, str):
                block[key] = block.get(key, "") + value
            else:
                block.setdefault(key, value)

    def blocks(self) -> list[dict[str, Any]] | None:
        if self._whole is not None:
            return valid_reasoning_blocks(self._whole)
        return valid_reasoning_blocks([self._parts[i] for i in sorted(self._parts)])


# The collector of the `stream_model_async` call running in this context.
reasoning_blocks_sink: contextvars.ContextVar[ReasoningBlocks | None] = contextvars.ContextVar(
    "js_reasoning_blocks_sink", default=None,
)


class _ObservedStream:
    """An SDK chunk stream that shows each chunk to ``sink`` on the way past."""

    def __init__(self, stream: Any, sink: ReasoningBlocks) -> None:
        self._stream = stream
        self._sink = sink

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    async def __aiter__(self):
        async for chunk in self._stream:
            self._sink.observe(chunk)
            yield chunk


def _stored_reasoning_blocks(message: ai.messages.Message) -> list[dict[str, Any]] | None:
    metadata = getattr(message, "provider_metadata", None) or {}
    return valid_reasoning_blocks((metadata.get(JS_METADATA_KEY) or {}).get("reasoning_blocks"))


class ReasoningContentProtocol(OpenAIChatCompletionsProtocol):
    """Adapt the SDK's reasoning field for local/custom chat-completions APIs.

    llama.cpp parses ``reasoning_content`` before applying its chat template.
    The client view is per stream; the provider still owns its real SDK client.

    With ``reasoning_blocks`` (a CLIProxyAPI route), an assistant message that
    carries Claude's thinking blocks sends them back as `reasoning_blocks`
    instead of `reasoning_content`. Every reply's blocks are collected into
    the call's `ReasoningBlocks`.
    """

    protocol_class_id: Literal["js_reasoning_content"] = "js_reasoning_content"
    reasoning_blocks: bool = False

    def stream(self, client: Any, model: ai.Model, messages: list[ai.messages.Message], **kwargs: Any):
        # The SDK turns each assistant message the history keeps into one
        # assistant entry, in order, so the n-th entry is the n-th message.
        per_assistant: list[list[dict[str, Any]] | None] = []
        if self.reasoning_blocks:
            kept, _issues = history_utils.drop_internal(messages)
            per_assistant = [_stored_reasoning_blocks(m) for m in kept if m.role == "assistant"]

        async def create(**api_kwargs: Any):
            entries = [dict(message) for message in api_kwargs["messages"]]
            assistants = [entry for entry in entries if entry.get("role") == "assistant"]
            blocks_by_entry = per_assistant if len(per_assistant) == len(assistants) else []
            for entry, blocks in zip(assistants, blocks_by_entry or [None] * len(assistants), strict=True):
                if blocks is not None:
                    entry["reasoning_blocks"] = blocks
                    entry.pop("reasoning", None)
                    entry.pop("reasoning_content", None)
                elif "reasoning" in entry:
                    entry.setdefault("reasoning_content", entry.pop("reasoning"))
            outgoing = entries
            api_kwargs["messages"] = outgoing
            response = await client.chat.completions.create(**api_kwargs)
            sink = reasoning_blocks_sink.get()
            if sink is None:
                return response
            if hasattr(response, "__aiter__"):
                return _ObservedStream(response, sink)
            sink.observe(response)
            return response

        view = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return super().stream(view, model, messages, **kwargs)


class _RedactedThinkingStream:
    """Expose the native snapshot and queue redacted blocks the ai parser skips."""

    def __init__(self, stream: Any, pending: list[Any]) -> None:
        self._stream = stream
        self._pending = pending

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    async def __aiter__(self):
        async for event in self._stream:
            if event.type == "content_block_start" and event.content_block.type == "redacted_thinking":
                block = event.content_block.model_dump(exclude_none=True)
                self._pending.extend([
                    ai.events.ReasoningStart(block_id=str(event.index)),
                    ai.events.ReasoningEnd(block_id=str(event.index), provider_metadata={
                        "anthropic": {"redacted_thinking": block}}),
                ])
            yield event


class AnthropicThinkingProtocol(AnthropicMessagesProtocol):
    """Round-trip opaque redacted blocks through ai 0.5.2's reasoning parts."""

    protocol_class_id: Literal["js_anthropic_thinking"] = "js_anthropic_thinking"
    explicit_prompt_cache: bool = False

    async def stream(self, client: Any, model: ai.Model, messages: list[ai.messages.Message], **kwargs: Any):
        redacted: dict[str, dict] = {}
        projected = []
        for message in messages:
            parts = []
            for part in message.parts:
                metadata = getattr(part, "provider_metadata", None) or {}
                block = (metadata.get("anthropic") or {}).get("redacted_thinking")
                if isinstance(part, ai.types.messages.ReasoningPart) and isinstance(block, dict):
                    # Let the SDK place the part through its normal serializer;
                    # replace this request-local marker before sending the body.
                    marker = uuid.uuid4().hex
                    redacted[marker] = block
                    part = ai.thinking("", provider_metadata={"anthropic": {"signature": marker}})
                parts.append(part)
            projected.append(message.model_copy(update={"parts": parts}))

        pending: list[Any] = []

        @asynccontextmanager
        async def open_message(**api_kwargs: Any):
            # An explicit message breakpoint covers the tools, system and history
            # prefix. CPA counts block breakpoints when enforcing its cache limit.
            cache_control = api_kwargs.pop("cache_control", None)
            if cache_control is not None and not self.explicit_prompt_cache:
                api_kwargs["extra_body"] = {
                    "cache_control": cache_control,
                    **(api_kwargs.get("extra_body") or {}),
                }
            elif cache_control is not None:
                for message in reversed(api_kwargs["messages"]):
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        content = [{"type": "text", "text": content}]
                        message["content"] = content
                    if isinstance(content, list):
                        block = next((block for block in reversed(content)
                                      if block.get("type") not in {"thinking", "redacted_thinking"}), None)
                        if block is not None:
                            block["cache_control"] = cache_control
                            break
            for message in api_kwargs["messages"]:
                content = message.get("content")
                if isinstance(content, list):
                    message["content"] = [redacted.get(block.get("signature"), block)
                                          if block.get("type") == "thinking" else block for block in content]
            async with client.messages.stream(**api_kwargs) as stream:
                yield _RedactedThinkingStream(stream, pending)

        view = SimpleNamespace(messages=SimpleNamespace(stream=open_message))
        async with aclosing(super().stream(view, model, projected, **kwargs)) as stream:
            async for event in stream:
                for redacted_event in pending:
                    yield redacted_event
                pending.clear()
                yield event


class _CheckedResponsesStream:
    def __init__(self, stream: Any, provider: str) -> None:
        self._stream = stream
        self._provider = provider

    async def __aiter__(self):
        async with self._stream:
            async for event in self._stream:
                kind = _field(event, "type")
                if kind in {"response.failed", "error"}:
                    error = (_field(_field(event, "response"), "error")
                             if kind == "response.failed" else event)
                    code = _field(error, "code")
                    body = event if isinstance(event, dict) else event.model_dump(exclude_none=True)
                    raise ai.ProviderAPIError(
                        _field(error, "message") or msgs.RESPONSES_STREAM_FAILED.words(),
                        provider=self._provider, body=body, code=code,
                        param=_field(error, "param"), error_type=_field(error, "type"),
                    )
                yield event


class ResponsesProtocol(OpenAIResponsesProtocol):
    """Keep Responses stream failures intact before SDK event normalization."""

    protocol_class_id: Literal["js_openai_responses"] = "js_openai_responses"

    def stream(self, client: Any, model: ai.Model, messages: list[ai.messages.Message], **kwargs: Any):
        async def create(**api_kwargs: Any):
            stream = await client.responses.create(**api_kwargs)
            return _CheckedResponsesStream(stream, kwargs["provider"])

        view = SimpleNamespace(responses=SimpleNamespace(create=create))
        return super().stream(view, model, messages, **kwargs)
