"""Canonical context-window accounting for js provider requests.

This module is intentionally independent of the runtime loop.  It knows how to
estimate model-visible request pieces, anchor estimates to the last provider
usage report, and answer the two budget questions the rest of js should ask:
"how large is the current context?" and "how many tokens remain before we must
compact?".
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any


_TOKENISH_RE = re.compile(r"[A-Za-z0-9_]+|[^\w\s]", re.UNICODE)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            max(0, self.input_tokens)
            + max(0, self.cache_read_tokens)
            + max(0, self.cache_write_tokens)
            + max(0, self.output_tokens)
        )


@dataclass(frozen=True)
class TokenEstimate:
    system_tokens: int = 0
    message_tokens: int = 0
    tool_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return max(0, self.system_tokens) + max(0, self.message_tokens) + max(0, self.tool_tokens)


@dataclass(frozen=True)
class BudgetStatus:
    current_context_tokens: int
    context_window: int | None
    output_reserve_tokens: int
    buffer_tokens: int
    effective_input_limit: int | None
    tokens_until_compaction: int | None
    should_compact: bool
    estimate: TokenEstimate
    used_provider_usage: bool


@dataclass(frozen=True)
class _UsageAnchor:
    usage: TokenUsage
    message_count: int
    system_tokens: int
    tool_tokens: int
    prefix_fingerprint: str | None = None


def _as_nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def usage_from_provider(usage: Any) -> TokenUsage:
    """Normalize provider/SDK usage objects without depending on their classes."""
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=_as_nonnegative_int(
            getattr(usage, "input_tokens", None)
            or getattr(usage, "prompt_tokens", None)
        ),
        cache_read_tokens=_as_nonnegative_int(
            getattr(usage, "cache_read_tokens", None)
            or getattr(usage, "cache_read_input_tokens", None)
        ),
        cache_write_tokens=_as_nonnegative_int(
            getattr(usage, "cache_write_tokens", None)
            or getattr(usage, "cache_creation_input_tokens", None)
        ),
        output_tokens=_as_nonnegative_int(
            getattr(usage, "output_tokens", None)
            or getattr(usage, "completion_tokens", None)
        ),
    )


def estimate_text_tokens(text: Any, *, chars_per_token: float = 4.0) -> int:
    """A conservative no-dependency estimator.

    Character/4 alone undercounts punctuation-heavy JSON and short identifiers.
    This combines that floor with a simple lexical pass and takes the larger
    value, while remaining deterministic and offline.
    """
    if text is None:
        return 0
    s = str(text)
    if not s:
        return 0
    ratio = chars_per_token if chars_per_token > 0 else 4.0
    char_floor = math.ceil(len(s) / ratio)
    lexical = 0
    for match in _TOKENISH_RE.finditer(s):
        piece = match.group(0)
        if len(piece) == 1 and not piece.isalnum() and piece != "_":
            lexical += 1
        elif piece.isascii():
            lexical += max(1, math.ceil(len(piece) / 6))
        else:
            lexical += max(1, math.ceil(len(piece) / 2))
    return max(1, char_floor, lexical)


def _jsonish(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return str(value)


def _messages_fingerprint(messages: list[Any] | tuple[Any, ...]) -> str:
    payload = _jsonish(list(messages))
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _part_tokens(part: Any, *, chars_per_token: float) -> int:
    if isinstance(part, str):
        return estimate_text_tokens(part, chars_per_token=chars_per_token)
    kind = getattr(part, "kind", None)
    if kind == "text":
        return estimate_text_tokens(getattr(part, "text", ""), chars_per_token=chars_per_token)
    if kind == "reasoning":
        return estimate_text_tokens(getattr(part, "text", ""), chars_per_token=chars_per_token)
    if kind == "tool_call":
        return (
            6
            + estimate_text_tokens(getattr(part, "tool_name", ""), chars_per_token=chars_per_token)
            + estimate_text_tokens(getattr(part, "tool_args", ""), chars_per_token=chars_per_token)
            + estimate_text_tokens(getattr(part, "tool_call_id", ""), chars_per_token=chars_per_token)
        )
    if kind == "tool_result":
        return (
            6
            + estimate_text_tokens(getattr(part, "tool_name", ""), chars_per_token=chars_per_token)
            + estimate_text_tokens(getattr(part, "tool_call_id", ""), chars_per_token=chars_per_token)
            + estimate_text_tokens(getattr(part, "result", ""), chars_per_token=chars_per_token)
        )
    if kind == "file":
        data = getattr(part, "data", None)
        data_len = len(data) if isinstance(data, (bytes, bytearray, str)) else 0
        media = getattr(part, "media_type", "")
        return estimate_text_tokens(f"{media} {data_len} bytes", chars_per_token=chars_per_token)
    return estimate_text_tokens(_jsonish(part), chars_per_token=chars_per_token)


def estimate_message_tokens(message: Any, *, chars_per_token: float = 4.0) -> int:
    """Estimate one history dict or ai SDK message."""
    role = ""
    content: Any = ""
    if isinstance(message, dict):
        role = str(message.get("role", ""))
        content = message.get("content", "")
        total = 4 + estimate_text_tokens(role, chars_per_token=chars_per_token)
        if isinstance(content, (list, tuple)):
            total += sum(_part_tokens(part, chars_per_token=chars_per_token) for part in content)
        else:
            total += estimate_text_tokens(content, chars_per_token=chars_per_token)
        reasoning = message.get("reasoning_content")
        if reasoning:
            total += estimate_text_tokens(reasoning, chars_per_token=chars_per_token)
        if role == "tool":
            total += estimate_text_tokens(message.get("tool_call_id", ""), chars_per_token=chars_per_token)
            total += estimate_text_tokens(message.get("name", ""), chars_per_token=chars_per_token)
        for call in message.get("tool_calls", []) or []:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            total += 8
            total += estimate_text_tokens(call.get("id", ""), chars_per_token=chars_per_token)
            total += estimate_text_tokens(fn.get("name", ""), chars_per_token=chars_per_token)
            total += estimate_text_tokens(fn.get("arguments", ""), chars_per_token=chars_per_token)
        return total

    role = str(getattr(message, "role", ""))
    total = 4 + estimate_text_tokens(role, chars_per_token=chars_per_token)
    for part in getattr(message, "parts", None) or []:
        total += _part_tokens(part, chars_per_token=chars_per_token)
    if not getattr(message, "parts", None):
        total += estimate_text_tokens(_jsonish(message), chars_per_token=chars_per_token)
    return total


def estimate_messages_tokens(messages: list[Any] | tuple[Any, ...], *, chars_per_token: float = 4.0) -> int:
    return sum(estimate_message_tokens(message, chars_per_token=chars_per_token) for message in messages)


def estimate_system_tokens(system: str | None, *, chars_per_token: float = 4.0) -> int:
    if not system:
        return 0
    return 4 + estimate_text_tokens(system, chars_per_token=chars_per_token)


def _tool_debug_payload(tool: Any) -> Any:
    if isinstance(tool, dict):
        return tool
    spec = getattr(tool, "spec", None)
    return {
        "name": getattr(tool, "name", ""),
        "description": getattr(spec, "description", ""),
        "parameters": getattr(spec, "params", {}),
    }


def estimate_tools_tokens(tools: Any, *, chars_per_token: float = 4.0) -> int:
    if not tools:
        return 0
    total = 0
    for tool in tools:
        payload = _tool_debug_payload(tool)
        total += 12 + estimate_text_tokens(_jsonish(payload), chars_per_token=chars_per_token)
    return total


def estimate_request_tokens(
    *,
    system: str = "",
    messages: list[Any] | tuple[Any, ...],
    tools: Any = None,
    chars_per_token: float = 4.0,
) -> TokenEstimate:
    return TokenEstimate(
        system_tokens=estimate_system_tokens(system, chars_per_token=chars_per_token),
        message_tokens=estimate_messages_tokens(messages, chars_per_token=chars_per_token),
        tool_tokens=estimate_tools_tokens(tools, chars_per_token=chars_per_token),
    )


class TokenState:
    """Provider-usage anchored token accounting for one conversation context."""

    def __init__(self, *, chars_per_token: float = 4.0) -> None:
        self.chars_per_token = chars_per_token if chars_per_token > 0 else 4.0
        self._anchor: _UsageAnchor | None = None

    def record_provider_usage(
        self,
        usage: Any,
        *,
        message_count: int,
        messages: list[Any] | tuple[Any, ...] | None = None,
        system: str = "",
        tools: Any = None,
    ) -> None:
        normalized = usage_from_provider(usage)
        if normalized.total_tokens <= 0:
            self._anchor = None
            return
        self._anchor = _UsageAnchor(
            usage=normalized,
            message_count=max(0, int(message_count)),
            system_tokens=estimate_system_tokens(system, chars_per_token=self.chars_per_token),
            tool_tokens=estimate_tools_tokens(tools, chars_per_token=self.chars_per_token),
            prefix_fingerprint=(
                _messages_fingerprint(messages[: max(0, int(message_count))])
                if messages is not None
                else None
            ),
        )

    @property
    def last_usage(self) -> TokenUsage | None:
        return self._anchor.usage if self._anchor is not None else None

    def reset(self) -> None:
        self._anchor = None

    def current_context_tokens(
        self,
        *,
        system: str = "",
        messages: list[Any] | tuple[Any, ...],
        tools: Any = None,
    ) -> tuple[int, TokenEstimate, bool]:
        estimate = estimate_request_tokens(
            system=system,
            messages=messages,
            tools=tools,
            chars_per_token=self.chars_per_token,
        )
        if self._anchor is None or self._anchor.message_count > len(messages):
            return estimate.total_tokens, estimate, False
        if self._anchor.prefix_fingerprint is not None:
            current_fingerprint = _messages_fingerprint(messages[: self._anchor.message_count])
            if current_fingerprint != self._anchor.prefix_fingerprint:
                return estimate.total_tokens, estimate, False

        delta_messages = messages[self._anchor.message_count :]
        delta_tokens = estimate_messages_tokens(delta_messages, chars_per_token=self.chars_per_token)
        current_system = estimate_system_tokens(system, chars_per_token=self.chars_per_token)
        current_tools = estimate_tools_tokens(tools, chars_per_token=self.chars_per_token)
        overhead_delta = (current_system - self._anchor.system_tokens) + (current_tools - self._anchor.tool_tokens)
        current = max(0, self._anchor.usage.total_tokens + delta_tokens + overhead_delta)
        return current, estimate, True

    def budget_status(
        self,
        *,
        system: str = "",
        messages: list[Any] | tuple[Any, ...],
        tools: Any = None,
        context_window: int | None,
        output_reserve_tokens: int = 0,
        buffer_tokens: int = 0,
    ) -> BudgetStatus:
        current, estimate, used_provider = self.current_context_tokens(
            system=system,
            messages=messages,
            tools=tools,
        )
        return budget_status_from_tokens(
            current_context_tokens=current,
            estimate=estimate,
            context_window=context_window,
            output_reserve_tokens=output_reserve_tokens,
            buffer_tokens=buffer_tokens,
            used_provider_usage=used_provider,
        )


def budget_status_from_tokens(
    *,
    current_context_tokens: int,
    estimate: TokenEstimate,
    context_window: int | None,
    output_reserve_tokens: int = 0,
    buffer_tokens: int = 0,
    used_provider_usage: bool = False,
) -> BudgetStatus:
    window = int(context_window) if context_window and context_window > 0 else None
    output = max(0, int(output_reserve_tokens or 0))
    buffer = max(0, int(buffer_tokens or 0))
    effective = None if window is None else max(0, window - output - buffer)
    remaining = None if effective is None else effective - max(0, int(current_context_tokens))
    return BudgetStatus(
        current_context_tokens=max(0, int(current_context_tokens)),
        context_window=window,
        output_reserve_tokens=output,
        buffer_tokens=buffer,
        effective_input_limit=effective,
        tokens_until_compaction=remaining,
        should_compact=remaining is not None and remaining < 0,
        estimate=estimate,
        used_provider_usage=used_provider_usage,
    )
