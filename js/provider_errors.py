"""A provider's failures, named: the errors a model call raises, the status and message each carries, which are worth a retry, and the rejections a resend can work around."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import ai
import httpx
import httpx2

from . import messages as msgs, routing

if TYPE_CHECKING:
    from .retry import StreamLimits


class FriendlyProviderError(routing.ProviderNotLoggedInError):
    """Provider-boundary failure already formatted for one-line user display."""


class RequestTimeoutError(ai.ProviderTimeoutError):
    """A model request ran out one of js's clocks (`retry.StreamLimits`);
    `setting` names the setting that set it, and its text says so."""

    setting = ""
    text = ""

    def __init__(self, seconds: float, *, provider: str | None = None, retryable: bool) -> None:
        super().__init__(self.text.format(seconds=seconds), provider=provider, is_retryable=retryable)
        self.seconds = seconds


class StreamIdleError(RequestTimeoutError):
    """A model request went `seconds` without a response byte after its first
    and was aborted. Retryable: the runtime sends the request again under its
    retry budget."""

    setting = "STREAM_IDLE_TIMEOUT"
    text = msgs.STREAM_IDLE_NO_BYTES

    def __init__(self, seconds: float, *, provider: str | None = None) -> None:
        super().__init__(seconds, provider=provider, retryable=True)


class FirstByteTimeoutError(RequestTimeoutError):
    """A model request went `seconds` from being sent without its first
    response byte. Not retried: a resend starts the prefill over."""

    setting = "FIRST_BYTE_TIMEOUT"
    text = msgs.FIRST_BYTE_NONE

    def __init__(self, seconds: float, *, provider: str | None = None) -> None:
        super().__init__(seconds, provider=provider, retryable=False)


class ConnectTimeoutError(RequestTimeoutError):
    """A model request's TCP or TLS handshake took longer than `seconds`.
    Retryable: no response came."""

    setting = "CONNECT_TIMEOUT"
    text = msgs.CONNECT_TIMED_OUT

    def __init__(self, seconds: float, *, provider: str | None = None) -> None:
        super().__init__(seconds, provider=provider, retryable=True)


# The SDK finish reason for a reply that hit its output-token cap: OpenAI chat
# "length" and Anthropic "max_tokens" both arrive as this.
OUTPUT_CAP_FINISH = "length"


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


def friendly_provider_error(
    exc: BaseException,
    *,
    provider_id: str | None,
) -> FriendlyProviderError | None:
    provider = _provider_label(provider_id, exc)
    detail = _detail(exc)

    if isinstance(exc, TypeError) and "Could not resolve authentication method" in str(exc):
        return FriendlyProviderError(msgs.PROVIDER_NO_AUTH_METHOD.said(provider=provider))

    if isinstance(exc, ValueError) and str(exc).startswith("unknown provider id:"):
        unknown = str(exc).partition(":")[2].strip()
        if unknown.startswith(("'", '"')) and len(unknown) >= 2:
            provider = unknown[1:-1]
        return FriendlyProviderError(msgs.PROVIDER_UNKNOWN.said(provider=provider))

    if isinstance(exc, ai.ProviderAuthenticationError):
        return FriendlyProviderError(msgs.PROVIDER_AUTH_FAILED.said(provider=provider, detail=detail))

    if isinstance(exc, ai.ProviderNotConfiguredError):
        return FriendlyProviderError(msgs.PROVIDER_NOT_CONFIGURED.said(provider=provider, detail=detail))

    # Keep request failures intact: runtime and summary recovery consume the
    # SDK type, retryability and structured overflow fields. The SDK diagnostic
    # is also more useful here than suggesting login for an oversized request.
    return None


# Anthropic's error types and the HTTP status each stands for. A stream that
# already answered 200 reports a failure as an `error` event, and the SDK
# raises it with the stream's status, 200. A proxy relays an upstream failure
# the same way.
_ERROR_TYPE_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "billing_error": 402,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "api_error": 500,
    "timeout_error": 504,
    "overloaded_error": 529,
}


def _error_detail(exc: BaseException) -> dict:
    """The provider's error object in ``exc``'s body: the `error` member of an
    Anthropic body, or the body itself (the OpenAI SDKs keep the inner object)."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return {}
    inner = body.get("error")
    return inner if isinstance(inner, dict) else body


def error_status(exc: BaseException) -> int | None:
    """The HTTP status ``exc`` stands for: its response's, or for an error
    event in a stream that answered 200, the status of the event's type."""
    status = getattr(getattr(exc, "http_context", None), "status_code", None)
    if not isinstance(status, int):
        return None
    if status < 400:
        return _ERROR_TYPE_STATUS.get(_error_detail(exc).get("type"), status)
    return status


def error_message(exc: BaseException) -> str:
    """The provider's own message in ``exc``: its body's `message`, else its
    error code, else the error's text, as one line."""
    message = _error_detail(exc).get("message")
    if not (isinstance(message, str) and message.strip()):
        message = getattr(exc, "code", None) or str(exc)
    return " ".join(str(message).split()) or type(exc).__name__


# The statuses a failed model request is sent again for: rate limited, a
# gateway that could not reach the server, Anthropic's overloaded. Every other
# status, 500 and every other 4xx included, fails at once.
RETRY_STATUSES = frozenset({429, 502, 503, 504, 529})


# The HTTP status an OpenAI Responses stream error's `code` stands for: the
# stream answered 200 and names its failure only by code.
_ERROR_CODE_STATUS = {
    "rate_limit_exceeded": 429,
    "server_error": 500,
    "internal_error": 500,
    "model_error": 500,
}


def failure_status(exc: BaseException) -> int | None:
    """The status a failed request stands for: `error_status`, else the
    status of its error code or error type."""
    status = error_status(exc)
    if status is not None:
        return status
    for name in (getattr(exc, "code", None), getattr(exc, "type", None)):
        if isinstance(name, str):
            status = _ERROR_CODE_STATUS.get(name) or _ERROR_TYPE_STATUS.get(name)
            if status is not None:
                return status
    return None


def retryable(exc: BaseException) -> bool:
    """Whether a failed model request is sent again: js's rule, the one every
    retrying caller reads through `is_retryable`. A status in RETRY_STATUSES
    is; a network failure is (refused, CONNECT_TIMEOUT, a connection dropped
    before or during the reply); a stream that went quiet past
    STREAM_IDLE_TIMEOUT is. FIRST_BYTE_TIMEOUT and every other answer the
    server gave are not."""
    if isinstance(exc, RequestTimeoutError):
        return bool(exc.is_retryable)
    status = failure_status(exc)
    if status is not None:
        return status in RETRY_STATUSES
    return isinstance(exc, ai.ProviderConnectionError)


def _connect_timeout(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: list[BaseException] = []
    while current is not None and all(current is not e for e in seen):
        if isinstance(current, (httpx2.ConnectTimeout, httpx.ConnectTimeout)):
            return True
        seen.append(current)
        current = current.__cause__ or current.__context__
    return False


def request_failure(exc: BaseException, *, provider: str | None, limits: StreamLimits) -> BaseException:
    """The error a failed model request leaves this module as, its
    `is_retryable` set by `retryable`. A handshake past CONNECT_TIMEOUT is a
    ConnectTimeoutError. The provider SDKs wrap transport errors raised while
    sending the request; one raised mid-stream (a peer closing inside a
    chunk) arrives as the raw httpx2 error and becomes the SDK's connection
    error."""
    if limits.connect is not None and _connect_timeout(exc):
        return ConnectTimeoutError(limits.connect, provider=provider)
    if isinstance(exc, httpx2.TransportError):
        kind = ai.ProviderTimeoutError if isinstance(exc, httpx2.TimeoutException) else ai.ProviderConnectionError
        exc = kind(f"{type(exc).__name__}: {exc}", provider=provider or "default")
    if isinstance(exc, ai.ProviderAPIError):
        exc.is_retryable = retryable(exc)
    return exc


# Anthropic's 400 for a replayed thinking block it will not accept: "Invalid
# `signature` in `thinking` block. The block is bound to a different
# conversation. ..." (the preserved-thinking prefix check), the same leading
# clause alone (an undecryptable signature), or "`thinking` or
# `redacted_thinking` blocks in the latest assistant message cannot be
# modified" (the last reply's blocks differ from the response, as when the
# `ai` SDK lost a redacted_thinking block).
_MODIFIED_THINKING_REJECTION = "in the latest assistant message cannot be modified"


_SIGNATURE_REJECTIONS = ("invalid `signature` in `thinking` block", "bound to a different conversation")


_SIGNED_REASONING_REJECTIONS = (
    *_SIGNATURE_REJECTIONS,
    _MODIFIED_THINKING_REJECTION,
    "expected `thinking` or `redacted_thinking`, but found",
)


# The Responses API's 400 for a replayed reasoning item it cannot decrypt (an
# item from another account or organization): code ``invalid_encrypted_content``,
# message "The encrypted content for item rs_... could not be verified" or
# "... could not be decrypted or parsed".
_ENCRYPTED_REASONING_CODE = "invalid_encrypted_content"


_ENCRYPTED_REASONING_REJECTIONS = ("could not be verified", "could not be decrypted")


# A 400 for a request carrying an image the model route cannot take, in each
# server's own words: omniroute, OpenAI, DeepSeek, llama.cpp, vLLM, Ollama.
_IMAGE_REJECTIONS = (
    "accepts text only",
    "image_url is only supported",
    "unknown variant `image_url`",
    "image input is not supported",
    "image inputs are not supported",
    "images are not supported",
    "does not support image",
    "do not support image",
    "does not support vision",
    "vision is not supported",
    "not a multimodal model",
)


def is_image_rejection(exc: BaseException) -> bool:
    """Whether ``exc`` is the provider refusing a request because it carries an
    image the model route cannot take. Routers send the same refusal as an
    HTTP 400 or as an error inside the stream, so any provider error counts."""
    if not isinstance(exc, ai.ProviderAPIError):
        return False
    haystack = " ".join(str(v) for v in (exc, getattr(exc, "body", None)) if v is not None).lower()
    return any(needle in haystack for needle in _IMAGE_REJECTIONS)


def is_signed_reasoning_rejection(exc: BaseException) -> bool:
    """Whether ``exc`` is the provider refusing a replayed signed reasoning
    block (an Anthropic thinking signature, an OpenAI Responses encrypted
    reasoning item)."""
    if not isinstance(exc, ai.ProviderBadRequestError):
        return False
    if getattr(exc, "code", None) == _ENCRYPTED_REASONING_CODE:
        return True
    haystack = " ".join(str(v) for v in (exc, getattr(exc, "body", None)) if v is not None).lower()
    if _ENCRYPTED_REASONING_CODE in haystack:
        return True
    if "encrypted content" in haystack and any(n in haystack for n in _ENCRYPTED_REASONING_REJECTIONS):
        return True
    return any(needle in haystack for needle in _SIGNED_REASONING_REJECTIONS)


def is_anthropic_thinking_rejection(exc: BaseException) -> bool:
    """Whether Anthropic refused the thinking blocks of a replayed turn."""
    if not isinstance(exc, ai.ProviderBadRequestError):
        return False
    haystack = f"{exc} {getattr(exc, 'body', '')}".lower()
    return any(needle in haystack for needle in _SIGNED_REASONING_REJECTIONS)


def is_signature_rejection(exc: BaseException) -> bool:
    """Whether Anthropic refused a thinking signature, which may belong to any
    earlier turn of the history."""
    haystack = f"{exc} {getattr(exc, 'body', '')}".lower()
    return (isinstance(exc, ai.ProviderBadRequestError)
            and any(needle in haystack for needle in _SIGNATURE_REJECTIONS))


def is_modified_thinking_rejection(exc: BaseException) -> bool:
    """Whether Anthropic requires restoration of the latest assistant turn."""
    haystack = f"{exc} {getattr(exc, 'body', '')}".lower()
    return is_anthropic_thinking_rejection(exc) and _MODIFIED_THINKING_REJECTION in haystack


def incomplete_reason_from_metadata(provider_metadata: Any) -> str | None:
    """Return a stable incomplete reason from provider metadata, if present.

    Responses streams surface ``response.incomplete`` as provider metadata on the
    assistant message / stream end. Keep this helper permissive so provider
    versions that expose either ``incomplete_reason`` or an OpenAI-shaped
    ``incomplete_details.reason`` do not make a truncated turn look like a clean
    stop again. Unknown-but-flagged incomplete streams get an explicit
    ``unknown`` reason rather than disappearing.
    """
    if not isinstance(provider_metadata, dict):
        return None
    for metadata in (provider_metadata, provider_metadata.get("openai")):
        if not isinstance(metadata, dict):
            continue
        reason = metadata.get("incomplete_reason")
        details = metadata.get("incomplete_details")
        if reason is None and isinstance(details, dict):
            reason = details.get("reason")
        if metadata.get("incomplete") or metadata.get("status") == "incomplete" or reason is not None:
            return str(reason or "unknown")
    return None


def incomplete_finish_reason(reason: str) -> str:
    return f"incomplete:{reason}"


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
    # llama.cpp: "request (N tokens) exceeds the available context size (M
    # tokens)", type "exceed_context_size_error". Neither matched any needle
    # above, so overflow recovery never fired on llama.cpp at all.
    "context size",
    "exceed_context_size",
    # Gemini: "The input token count (N) exceeds the maximum number of tokens
    # allowed (M)".
    "exceeds the maximum number of tokens",
)


# A rate limit is never an overflow, whatever its text says: OpenAI's TPM limit
# reads "Request too large for <model> ... on tokens per min (TPM)" and would
# match the "request too large" needle. Recovering a rate limit by clearing
# tool results and summarizing destroys context for nothing.
_RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "rate_limit_error", "rate_limited"})


_RATE_LIMIT_NEEDLES = ("rate limit", "rate_limit", "tokens per min", "requests per min")


# HTTP 413 named in a message that came without a status (SDK text like
# "Error code: 413 - ..."). A bare substring test also hit request ids
# (req_84133), token counts and ports.
_STATUS_413_RE = re.compile(r"\b(?:error code|status(?: code)?|http)[:\s]+413\b")


def _status_code(exc: BaseException) -> int | None:
    context = getattr(exc, "http_context", None)
    status = getattr(context, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def provider_error_fields(exc: BaseException) -> dict[str, Any]:
    return {
        "code": getattr(exc, "code", None),
        "type": getattr(exc, "type", None),
        "status_code": _status_code(exc),
        "body": getattr(exc, "body", None),
    }


def is_context_overflow_error(exc: BaseException) -> bool:
    if not isinstance(exc, ai.ProviderAPIError):
        return False
    fields = provider_error_fields(exc)
    status = fields["status_code"]
    code = str(fields["code"] or "").lower()
    if status == 429 or isinstance(exc, ai.ProviderRateLimitError) or code in _RATE_LIMIT_CODES:
        return False
    if status == 413 or isinstance(exc, ai.ProviderRequestTooLargeError):
        return True
    haystack = " ".join(
        [str(exc), *(str(value) for value in fields.values() if value is not None)]
    ).lower()
    if any(needle in haystack for needle in _RATE_LIMIT_NEEDLES):
        return False
    if _STATUS_413_RE.search(haystack):
        return True
    return any(needle in haystack for needle in _CONTEXT_OVERFLOW_NEEDLES)


class SilentOverflowError(ai.ProviderAPIError):
    """A reply the provider accepted although its prompt exceeded the context
    window: the provider cut the input to fit without an error."""

    def __init__(self, prompt_tokens: int, context_window: int | None) -> None:
        super().__init__(
            f"provider reported {prompt_tokens} input tokens, over the {context_window}-token context window",
            code="silent_context_overflow", is_retryable=False,
        )
        self.prompt_tokens = prompt_tokens
        self.context_window = context_window


def is_max_output_incomplete(reason: object) -> bool:
    text = str(reason or "").lower()
    return text in {"max_output_tokens", "max_tokens", "length"} or "max_output" in text
