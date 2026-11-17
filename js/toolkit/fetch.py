"""The fetch tool: an http, https or file:// URL read into the result, or
streamed to a file with save=."""

from __future__ import annotations

import codecs
import html
import json
import mimetypes
import os
import re
import secrets
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import output, paths
from ..tool_binaries import (
    ARIA2_EXECUTABLE,
    DownloadError,
    download_with_aria2,
    resolve_binary,
    warn_urllib_fallback,
)
from .core import Tool, ToolContext
from .descriptions import load_description
from .fs import _detect_visual_mime, _image_marker, _read_regular_bytes
from .presentation import Presentation
from .results import spill_oversized_result
from .sanitize import text_or_default
from .web_search import _absolutize

_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_RE = re.compile(r"(?is)<a\b(?P<attrs>[^>]*)>(?P<label>.*?)</a\s*>")
_HREF_RE = re.compile(
    r"(?is)(?:^|\s)href\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))"
)
# Structural boundaries, so adjacent blocks do not fuse into one word when the
# remaining tags are swept away below. A block ends a line; a table cell ends a
# space, so `<td>A</td><td>B</td>` reads as "A B" rather than "AB".
_BLOCK_CLOSE_RE = re.compile(
    r"(?i)</(?:div|h[1-6]|li|tr|ul|ol|dl|dt|dd|blockquote|pre|section|article|"
    r"header|footer|aside|main|nav|figure|figcaption|form|table|title)\s*>|<hr\s*/?>"
)
_CELL_CLOSE_RE = re.compile(r"(?i)</t[dh]\s*>")


def _html_to_text(raw: str, base_url: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", raw)

    def _anchor_to_markdown(match: re.Match[str]) -> str:
        href_match = _HREF_RE.search(match.group("attrs"))
        if href_match is None:
            return match.group("label")
        href = next(value for value in href_match.group("double", "single", "bare") if value is not None)
        return f"[{match.group('label')}]({href})"

    text = _ANCHOR_RE.sub(_anchor_to_markdown, text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = _CELL_CLOSE_RE.sub(" ", text)
    text = _BLOCK_CLOSE_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _absolutize(text, base_url)
    lines = [line.strip() for line in text.splitlines()]
    compact: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                compact.append("")
            blank = True
        else:
            compact.append(line)
            blank = False
    return "\n".join(compact).strip()


# What may land in RAM for an INLINE result. A saved download is a different
# question entirely: it streams to disk and is bounded by DOWNLOAD_MAX,
# which defaults to unlimited. A 6GB ISO is a normal thing to fetch(save=...).
_DOWNLOAD_MAX_BYTES = 32 * 1024 * 1024
_STREAM_CHUNK = 1024 * 1024
_INLINE_READ_CHUNK = 64 * 1024
_SNIFF_BYTES = 4096


class _FetchTimeoutError(Exception):
    pass


def _inline_read_limit_error() -> str:
    return (
        f'ERROR: response exceeds {_DOWNLOAD_MAX_BYTES} byte inline read limit; use save= to stream the full response to disk'
    )


def _download_limit(context: ToolContext) -> int | None:
    """The configured save= ceiling, or None for unlimited (the default)."""
    limit = int(getattr(context, "max_download_bytes", 0) or 0)
    return limit if limit > 0 else None


def _stream_download(
    read_chunk: Any,
    target: Path,
    content_type: str,
    context: ToolContext,
    limit: int | None,
    expected_size: int | None = None,
) -> str:
    """Move bytes to disk without ever holding the whole transfer in memory.

    Writes to a hidden sibling and publishes with os.replace, so an interrupted
    or over-limit transfer never leaves a half-file at the name the caller asked
    for. This is what lets max_download_bytes default to unlimited.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.parent / f".{target.name}.js-partial-{secrets.token_hex(6)}"
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    written = 0
    try:
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = read_chunk(_STREAM_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if limit is not None and written > limit:
                        raise DownloadError(f"response exceeds {limit} byte download limit")
                    out.write(chunk)
                if expected_size is not None and written != expected_size:
                    raise DownloadError(f"incomplete download: expected {expected_size} bytes, received {written}")
        except DownloadError as exc:
            return f"ERROR: {exc}"
        except OSError as exc:
            return f"ERROR: {type(exc).__name__}: {exc}"
        context.snapshot(target)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return (
        f"SAVED_RESPONSE path={target} size={written} bytes "
        f"content-type={content_type or 'unknown'}"
    )
_TEXT_MEDIA_TYPES = {
    "application/csv",
    "application/ecmascript",
    "application/javascript",
    "application/json",
    "application/ld+json",
    "application/rtf",
    "application/x-ndjson",
    "application/x-www-form-urlencoded",
    "application/xhtml+xml",
    "application/xml",
}


def _header_value(headers: Any, name: str) -> str:
    getter = getattr(headers, "get", None)
    if getter is not None:
        for candidate in (name, name.lower(), name.title()):
            value = getter(candidate)
            if value is not None:
                return str(value)
    if isinstance(headers, Mapping):
        lowered = name.lower()
        for key, value in headers.items():
            if str(key).lower() == lowered:
                return str(value)
    return ""


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _looks_binary(data: bytes) -> bool:
    sample = data[:_SNIFF_BYTES]
    if b"\x00" in sample:
        return True
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=len(data) <= _SNIFF_BYTES)
    except UnicodeDecodeError:
        return True
    controls = sum(byte < 32 and byte not in {9, 10, 12, 13} for byte in sample)
    return bool(sample) and controls / len(sample) > 0.30


def _is_text_response(content_type: str, data: bytes) -> bool:
    media_type = _media_type(content_type)
    if media_type.startswith("text/") or media_type in _TEXT_MEDIA_TYPES:
        return True
    if media_type.endswith("+json") or media_type.endswith("+xml"):
        return True
    if not media_type:
        return not _looks_binary(data)
    return False


def _image_mime(path: Path | None, content_type: str, data: bytes) -> str | None:
    media_type = _media_type(content_type)
    if media_type.startswith("image/"):
        return media_type
    detected = _detect_visual_mime(path or Path("fetched"), data[:32])
    if detected and detected.startswith("image/"):
        return detected
    return None


def _guess_file_content_type(path: Path, data: bytes) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    detected = _detect_visual_mime(path, data[:32])
    if detected:
        return detected
    if _looks_binary(data):
        return "application/octet-stream"
    return guessed or "text/plain"


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    lowered = name.lower()
    for existing in list(headers):
        if existing.lower() == lowered:
            del headers[existing]
    headers[name] = value


def _normalize_headers(headers: Any, user_agent: str) -> dict[str, str] | str:
    normalized: dict[str, str] = {}
    if headers is None:
        pass
    elif isinstance(headers, Mapping):
        for key, value in headers.items():
            name = str(key).strip()
            text = "" if value is None else str(value).strip()
            if not name or "\r" in name or "\n" in name or "\r" in text or "\n" in text:
                return "ERROR: headers must not contain empty names or newlines"
            _set_header(normalized, name, text)
    elif isinstance(headers, (list, tuple)):
        for item in headers:
            if not isinstance(item, str) or ":" not in item:
                return "ERROR: headers list entries must be strings like 'Name: value'"
            name, value = item.split(":", 1)
            name = name.strip()
            value = value.strip()
            if not name or "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                return "ERROR: headers must not contain empty names or newlines"
            _set_header(normalized, name, value)
    else:
        return "ERROR: headers must be a mapping or a list of 'Name: value' strings"
    if not any(key.lower() == "user-agent" for key in normalized):
        normalized["User-Agent"] = user_agent
    return normalized


def _request_body(headers: dict[str, str], body: str | None, json_body: Any) -> bytes | str | None:
    if json_body is not None:
        if body is not None:
            return "ERROR: pass either body or json_body, not both"
        if not any(key.lower() == "content-type" for key in headers):
            headers["Content-Type"] = "application/json"
        return json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if body is not None:
        return text_or_default(body).encode("utf-8")
    return None


def _download_target(save: str | None, context: ToolContext) -> Path | None:
    return context.resolve_path(save, write=True) if save else None


def _content_length(headers: Any) -> int | None:
    value = _header_value(headers, "content-length")
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


def _aria_eligible(method: str, data: bytes | None, scheme: str) -> bool:
    # Requests with bodies are API round-trips even if their response is saved.
    return method == "GET" and data is None and scheme in {"http", "https"}


def _saved_content_type(
    url: str,
    target: Path,
    headers: dict[str, str],
    timeout_s: float,
) -> str:
    """Preserve the saved-response media type without making a second byte transfer."""
    request = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            content_type = _header_value(response.headers, "content-type")
            if content_type:
                return content_type
    except Exception:  # noqa: BLE001 -- a HEAD probe must never invalidate a good download
        pass
    with target.open("rb") as stream:
        sample = stream.read(_SNIFF_BYTES + 1)
    return _guess_file_content_type(target, sample)


def _aria_payload(
    binary: str,
    url: str,
    *,
    headers: dict[str, str],
    content_type: str,
    response_url: str,
    raw: bool | None,
    context: ToolContext,
    deadline: float,
) -> str:
    with tempfile.TemporaryDirectory(prefix="js-fetch-", dir=paths.tmp_dir()) as raw_temp:
        destination = Path(raw_temp) / "response"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _FetchTimeoutError
        download_with_aria2(
            binary,
            url,
            destination,
            timeout_s=remaining,
            headers=headers,
            max_bytes=_DOWNLOAD_MAX_BYTES,
        )
        payload = destination.read_bytes()
    return _format_payload(
        data=payload,
        content_type=content_type,
        raw=raw,
        context=context,
        truncated=len(payload) > context.max_tool_result_bytes,
        base_url=response_url,
    )


def _descriptor(kind: str, content_type: str, size: int, truncated: bool) -> str:
    suffix = " [truncated]" if truncated else ""
    return f"{kind} content-type={content_type or 'unknown'} size={size} bytes{suffix}"


def _temp_image_path(mime: str, data: bytes) -> Path:
    suffix = mimetypes.guess_extension(mime) or ".img"
    with tempfile.NamedTemporaryFile(prefix="js-fetch-", suffix=suffix, delete=False, dir=paths.tmp_dir()) as handle:
        handle.write(data)
        return Path(handle.name)


def _format_payload(
    *,
    data: bytes,
    content_type: str,
    raw: bool | None,
    context: ToolContext,
    truncated: bool = False,
    source_path: Path | None = None,
    total_size: int | None = None,
    base_url: str = "",
) -> str:
    size = len(data) if total_size is None else total_size
    image_mime = _image_mime(source_path, content_type, data)
    if image_mime:
        # `truncated` is the text budget; an image arrives here whole, and an
        # image over imagefit's limits is fitted, not refused.
        if context.vision_enabled:
            marker_path = source_path or _temp_image_path(image_mime, data)
            return _image_marker(marker_path, image_mime, size, data)
        return _descriptor("IMAGE_RESPONSE", image_mime, size, truncated)

    if not _is_text_response(content_type, data):
        return _descriptor("BINARY_RESPONSE", content_type, size, truncated)

    text = data.decode("utf-8", errors="replace")
    if not raw and "html" in _media_type(content_type):
        text = _html_to_text(text, base_url)
    if truncated:
        # Fetch must retain the tail itself: the generic runtime spill happens
        # after the handler returns, which is too late if only cap+1 bytes were
        # read. Reuse that spill function so fetch has the same directory,
        # content-addressed name, preview, and pointer as every other large tool
        # result. Prefer the ordinary inline cap when it is the tighter bound;
        # this also prevents the runtime from spilling the pointer a second time.
        hard_cap = max(0, int(context.max_tool_result_bytes))
        inline_cap = max(0, int(getattr(context, "max_tool_result_inline_bytes", 0) or 0))
        spill_cap = inline_cap if inline_cap and (not hard_cap or inline_cap < hard_cap) else hard_cap
        limit_name = (
            "TOOL_RESULT_INLINE_MAX"
            if spill_cap == inline_cap and inline_cap
            else "TOOL_RESULT_MAX"
        )
        return spill_oversized_result(text, spill_cap, limit_name=limit_name, force=True)
    return text


def _response_socket(resp: Any) -> Any | None:
    """Return urllib's underlying socket when its response exposes one."""
    stream = getattr(resp, "fp", None)
    raw = getattr(stream, "raw", None)
    return getattr(raw, "_sock", None)


def _read_response(
    resp: Any, limit: int, *, deadline: float | None = None
) -> tuple[bytes, bool]:
    if deadline is None or not hasattr(resp, "read1"):
        data = resp.read(limit + 1)
        return data, len(data) > limit

    chunks: list[bytes] = []
    retained = 0
    sock = _response_socket(resp)
    while retained <= limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _FetchTimeoutError
        # http.client closes the socket itself once Content-Length bytes have
        # arrived; a settimeout on that closed socket raises EBADF, which used
        # to surface as "ERROR: OSError: [Errno 9]" on every complete response.
        if sock is not None:
            try:
                sock.settimeout(remaining)
            except OSError:
                sock = None
        try:
            chunk = resp.read1(min(_INLINE_READ_CHUNK, limit + 1 - retained))
        except TimeoutError as exc:
            raise _FetchTimeoutError from exc
        if not chunk:
            break
        chunks.append(chunk)
        retained += len(chunk)
    data = b"".join(chunks)
    return data, len(data) > limit


def _fetch_file_url(
    url: str,
    *,
    raw: bool | None,
    save_target: Path | None,
    context: ToolContext,
) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc and parsed.netloc not in {"localhost", "127.0.0.1"}:
        return f"ERROR: unsupported file:// host {parsed.netloc!r}"
    path = context.resolve_path(urllib.request.url2pathname(parsed.path))
    try:
        size = path.stat().st_size
        if save_target is not None:
            # Copying a local file to another path is a transfer, not a read: it
            # streams, so the size of the thing is the caller's business. The
            # head read doubles as the non-regular-file guard.
            head = _read_regular_bytes(path, _SNIFF_BYTES + 1)
            content_type = _guess_file_content_type(path, head)
            with path.open("rb") as source:
                return _stream_download(
                    source.read, save_target, content_type, context, _download_limit(context)
                )

        # Read the whole file, not just cap+1: an oversized text response is
        # spilled in full below, and the spill is only honest if the tail was
        # actually read. _DOWNLOAD_MAX_BYTES bounds that so a multi-GB log does
        # not land in RAM, and _read_regular_bytes refuses devices/FIFOs whose
        # st_size is 0 and whose read never reaches EOF.
        if size > _DOWNLOAD_MAX_BYTES:
            return f'ERROR: file exceeds {_DOWNLOAD_MAX_BYTES} byte read limit; use save= or fs_read'
        data = _read_regular_bytes(path, _DOWNLOAD_MAX_BYTES + 1)
        if len(data) > _DOWNLOAD_MAX_BYTES:
            return f'ERROR: file exceeds {_DOWNLOAD_MAX_BYTES} byte read limit; use save= or fs_read'
        truncated = len(data) > context.max_tool_result_bytes
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"
    content_type = _guess_file_content_type(path, data)
    return _format_payload(
        data=data,
        content_type=content_type,
        raw=raw,
        context=context,
        truncated=truncated,
        source_path=path,
        total_size=size,
        base_url=url,
    )


def fetch(
    url: str,
    raw: bool | None = False,
    method: str | None = "GET",
    headers: Any = None,
    body: str | None = None,
    json_body: Any = None,
    save: str | None = None,
    context: ToolContext | None = None,
    *,
    download_resolver=None,
) -> str:
    if context is None:
        return "ERROR: missing ToolContext"
    try:
        method_name = (text_or_default(method, "GET") or "GET").upper()
        normalized_headers = _normalize_headers(headers, context.user_agent)
        if isinstance(normalized_headers, str):
            return normalized_headers
        data = _request_body(normalized_headers, body, json_body)
        if isinstance(data, str):
            return data
        save_target = _download_target(save, context)
        url = urllib.parse.unwrap(url)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https", "file"}:
            return f"ERROR: unsupported URL scheme {parsed.scheme!r}; expected http, https, or file"
        if parsed.scheme == "file":
            if method_name != "GET":
                return "ERROR: file:// fetch only supports GET"
            return _fetch_file_url(url, raw=raw, save_target=save_target, context=context)

        aria_eligible = _aria_eligible(method_name, data, parsed.scheme)
        resolver = resolve_binary if download_resolver is None else download_resolver
        aria2c = resolver(ARIA2_EXECUTABLE) if aria_eligible else None
        if save_target is not None and aria2c is not None:
            download_started = time.monotonic()
            download_with_aria2(
                aria2c,
                url,
                save_target,
                timeout_s=context.download_timeout_s,
                headers=normalized_headers,
                max_bytes=_download_limit(context),
                before_publish=lambda: context.snapshot(save_target),
            )
            remaining = context.download_timeout_s - (time.monotonic() - download_started)
            if remaining > 0:
                content_type = _saved_content_type(
                    url,
                    save_target,
                    normalized_headers,
                    min(context.fetch_timeout_s, remaining),
                )
            else:
                with save_target.open("rb") as stream:
                    content_type = _guess_file_content_type(save_target, stream.read(_SNIFF_BYTES + 1))
            return (
                f"SAVED_RESPONSE path={save_target} size={save_target.stat().st_size} bytes "
                f"content-type={content_type or 'unknown'}"
            )
        if save_target is not None and aria_eligible:
            warn_urllib_fallback("fetch(save=...)", emit=output.notice)

        req = urllib.request.Request(
            url,
            data=data,
            headers=normalized_headers,
            method=method_name,
        )
        limit = context.max_tool_result_bytes
        # A download is bounded by size and by download_timeout_s, not by page-load
        # latency; sharing fetch_timeout_s silently demanded ~2 MB/s to move anything large.
        timeout_s = context.download_timeout_s if save_target else context.fetch_timeout_s
        request_deadline = time.monotonic() + timeout_s if save_target is None else None
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            content_type = _header_value(resp.headers, "content-type")
            if save_target is not None:
                # Stream: a save must not be bounded by what fits in memory.
                return _stream_download(
                    resp.read, save_target, content_type, context, _download_limit(context),
                    expected_size=_content_length(resp.headers) if method_name != "HEAD" else None,
                )
            response_length = _content_length(resp.headers)
            transfer_from_headers = aria2c is not None and (
                (bool(content_type) and not _is_text_response(content_type, b""))
                or (
                    response_length is not None
                    and response_length > min(limit, _DOWNLOAD_MAX_BYTES)
                )
            )
            if transfer_from_headers:
                payload = b""
                truncated = True
            elif aria2c is not None:
                payload, truncated = _read_response(
                    resp, min(limit, _DOWNLOAD_MAX_BYTES), deadline=request_deadline
                )
            else:
                payload, too_large = _read_response(
                    resp, _DOWNLOAD_MAX_BYTES, deadline=request_deadline
                )
                truncated = len(payload) > limit
                is_transfer = truncated or not _is_text_response(content_type, payload)
                if is_transfer:
                    warn_urllib_fallback("fetch() response transfer", emit=output.notice)
                if too_large and is_transfer:
                    return _inline_read_limit_error()
            response_url = str(getattr(resp, "geturl", lambda: url)() or url)
        if aria2c is not None and (
            transfer_from_headers
            or truncated
            or not _is_text_response(content_type, payload)
        ):
            if response_length is not None and response_length > _DOWNLOAD_MAX_BYTES:
                return _inline_read_limit_error()
            return _aria_payload(
                aria2c,
                url,
                headers=normalized_headers,
                content_type=content_type,
                response_url=response_url,
                raw=raw,
                context=context,
                deadline=request_deadline,
            )
        return _format_payload(
            data=payload,
            content_type=content_type,
            raw=raw,
            context=context,
            truncated=truncated,
            base_url=response_url,
        )
    except _FetchTimeoutError:
        return f"ERROR: fetch timed out after {context.fetch_timeout_s} seconds"
    except DownloadError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"


def _fetch_is_read_only(args: dict[str, Any]) -> bool:
    """A GET or HEAD that saves nothing."""
    method = (text_or_default(args.get("method"), "GET") or "GET").upper()
    return method in {"GET", "HEAD"} and not args.get("save")


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "fetch",
            load_description("fetch"),
            fetch,
            {
                "url": {"type": "string"},
                "raw": {"type": "boolean", "default": False},
                "method": {"type": "string", "default": "GET"},
                "headers": {
                    "anyOf": [
                        {"type": "object", "additionalProperties": {"type": "string"}},
                        {"type": "array", "items": {"type": "string"}},
                    ]
                },
                "body": {"type": "string"},
                "json_body": {"type": "object", "additionalProperties": True},
                "save": {"type": "string"},
            },
            required=("url",),
            read_only_when=_fetch_is_read_only,
            presentation=Presentation(key=("url",), fold_noun="page"),
        ),
    )
