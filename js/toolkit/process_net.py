"""Shell and network tools."""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import settings as _settings
from ..capped_process import CappedProcessResult, _run_capped, truncation_marker
from ..tool_binaries import (
    ARIA2_EXECUTABLE,
    DownloadError,
    download_with_aria2,
    resolve_binary,
    warn_urllib_fallback,
)
from .core import Tool, ToolContext
from .descriptions import load_description
from .fs import _detect_visual_mime, _image_marker
from .sanitize import int_or_default, text_or_default
from .search import _absolutize


_ENV_ALLOW = _settings.DEFAULT_SHELL_ENV_ALLOW
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_RE = re.compile(r"(?is)<a\b(?P<attrs>[^>]*)>(?P<label>.*?)</a\s*>")
_HREF_RE = re.compile(
    r"(?is)\bhref\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))"
)



def _default_shell() -> str:
    if sys.platform == "win32":
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL", "/bin/sh")


def shell(
    command: str,
    cwd: str | None = None,
    timeout: int = 300,
    keep_ansi: bool = False,
    env: list[str] | None = None,
    description: str | None = None,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    command = text_or_default(command)
    timeout = int_or_default(timeout, 300, minimum=1)
    workdir = context.resolve_path(cwd) if cwd else context.cwd
    configured_allow = getattr(context, "shell_env_allow", _ENV_ALLOW)
    if not isinstance(configured_allow, (list, tuple, set, frozenset)):
        configured_allow = _ENV_ALLOW
    allowed = {str(key) for key in configured_allow if str(key)} | set(env or [])
    safe_env = {key: os.environ[key] for key in allowed if key in os.environ}
    shell_path = _default_shell()
    shell_arg = "/C" if sys.platform == "win32" else "-c"
    # A command can create/edit/delete anything, so memoized fs_search results are
    # no longer trustworthy once one has run.
    context.invalidate_search_cache()
    try:
        result = _run_capped(
            [shell_path, shell_arg, command],
            timeout=timeout,
            cwd=str(workdir),
            env=safe_env,
            cap=context.max_bash_output_bytes,
        )
        if isinstance(result, CappedProcessResult):
            returncode, raw_stdout, raw_stderr = result.returncode, result.stdout, result.stderr
        else:
            returncode, raw_stdout, raw_stderr = result
    except subprocess.TimeoutExpired as expired:
        # _run_capped attaches whatever the process had already written. Throwing
        # it away told the model nothing about a build that printed 200 lines and
        # then hung -- the last lines before the hang are the whole diagnosis.
        parts = [f"ERROR: command timed out after {timeout}s"]
        for label, raw in (("stdout", expired.output), ("stderr", expired.stderr)):
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            if not keep_ansi:
                text = _ANSI_RE.sub("", text)
            text = text.strip()
            if text:
                parts.append(f"--- {label} before the timeout ---\n{text}")
        if len(parts) == 1:
            parts.append("(the command produced no output before it was killed)")
        return "\n".join(parts)
    except OSError as exc:
        return f"ERROR: {exc}"

    stdout = raw_stdout.decode("utf-8", errors="replace")
    stderr = raw_stderr.decode("utf-8", errors="replace")
    if not keep_ansi:
        stdout = _ANSI_RE.sub("", stdout)
        stderr = _ANSI_RE.sub("", stderr)
    if isinstance(result, CappedProcessResult):
        marker = truncation_marker(context.max_bash_output_bytes)
        if result.stdout_truncated:
            stdout = f"{stdout}\n{marker}" if stdout else marker
        if result.stderr_truncated:
            stderr = f"{stderr}\n{marker}" if stderr else marker
    parts = [f"shell={shell_path}", f"exit={returncode}"]
    if description:
        parts.append(f"description={description}")
    if returncode:
        allowed_names = ",".join(sorted(allowed)) or "<none>"
        present_names = ",".join(sorted(safe_env)) or "<none>"
        parts.append(
            "environment=filtered "
            f"allowed={allowed_names} present={present_names}; "
            "names not allowed by limits.shell_env_allow or the env parameter are unset"
        )
    if stdout:
        parts.append(f"--- stdout ---\n{stdout}")
    if stderr:
        parts.append(f"--- stderr ---\n{stderr}")
    if not stdout and not stderr:
        parts.append("(no output)")
    return "\n".join(parts)


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


_DOWNLOAD_MAX_BYTES = 32 * 1024 * 1024
_DEFAULT_USER_AGENT = "js-agent/0.1"
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
    sample = data[:4096]
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
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


def _normalize_headers(headers: Any) -> dict[str, str] | str:
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
        normalized["User-Agent"] = _DEFAULT_USER_AGENT
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
    return context.resolve_path(save) if save else None


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
        sample = stream.read(4096)
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
) -> str:
    with tempfile.TemporaryDirectory(prefix="js-fetch-") as raw_temp:
        destination = Path(raw_temp) / "response"
        download_with_aria2(
            binary,
            url,
            destination,
            timeout_s=context.download_timeout_s,
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


def _write_download(target: Path, data: bytes, content_type: str, context: ToolContext) -> str:
    context.snapshot(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return f"SAVED_RESPONSE path={target} size={len(data)} bytes content-type={content_type or 'unknown'}"


def _descriptor(kind: str, content_type: str, size: int, truncated: bool) -> str:
    suffix = " [truncated]" if truncated else ""
    return f"{kind} content-type={content_type or 'unknown'} size={size} bytes{suffix}"


def _temp_image_path(mime: str, data: bytes) -> Path:
    suffix = mimetypes.guess_extension(mime) or ".img"
    with tempfile.NamedTemporaryFile(prefix="js-fetch-", suffix=suffix, delete=False) as handle:
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
        if context.vision_enabled and not truncated:
            marker_path = source_path or _temp_image_path(image_mime, data)
            return _image_marker(marker_path, image_mime, size)
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
        from ..runtime import spill_oversized_result

        hard_cap = max(0, int(context.max_tool_result_bytes))
        inline_cap = max(0, int(getattr(context, "max_tool_result_inline_bytes", 0) or 0))
        spill_cap = inline_cap if inline_cap and (not hard_cap or inline_cap < hard_cap) else hard_cap
        limit_name = (
            "limits.max_tool_result_inline_bytes"
            if spill_cap == inline_cap and inline_cap
            else "limits.max_tool_result_bytes"
        )
        return spill_oversized_result(text, spill_cap, limit_name=limit_name, force=True)
    return text


def _read_response(resp: Any, limit: int) -> tuple[bytes, bool]:
    data = resp.read(limit + 1)
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
    path = Path(urllib.request.url2pathname(parsed.path))
    try:
        size = path.stat().st_size
        if save_target is not None:
            if size > _DOWNLOAD_MAX_BYTES:
                return f"ERROR: response exceeds {_DOWNLOAD_MAX_BYTES} byte download limit"
            data = path.read_bytes()
            content_type = _guess_file_content_type(path, data)
            return _write_download(save_target, data, content_type, context)

        data = path.read_bytes()
        truncated = size > context.max_tool_result_bytes
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
) -> str:
    if context is None:
        return "ERROR: missing ToolContext"
    try:
        method_name = (text_or_default(method, "GET") or "GET").upper()
        normalized_headers = _normalize_headers(headers)
        if isinstance(normalized_headers, str):
            return normalized_headers
        data = _request_body(normalized_headers, body, json_body)
        if isinstance(data, str):
            return data
        save_target = _download_target(save, context)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file":
            if method_name != "GET":
                return "ERROR: file:// fetch only supports GET"
            return _fetch_file_url(url, raw=raw, save_target=save_target, context=context)

        aria_eligible = _aria_eligible(method_name, data, parsed.scheme)
        aria2c = resolve_binary(ARIA2_EXECUTABLE) if aria_eligible else None
        if save_target is not None and aria2c is not None:
            download_started = time.monotonic()
            download_with_aria2(
                aria2c,
                url,
                save_target,
                timeout_s=context.download_timeout_s,
                headers=normalized_headers,
                max_bytes=_DOWNLOAD_MAX_BYTES,
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
                    content_type = _guess_file_content_type(save_target, stream.read(4096))
            return (
                f"SAVED_RESPONSE path={save_target} size={save_target.stat().st_size} bytes "
                f"content-type={content_type or 'unknown'}"
            )
        if save_target is not None and aria_eligible:
            warn_urllib_fallback("fetch(save=...)")

        req = urllib.request.Request(
            url,
            data=data,
            headers=normalized_headers,
            method=method_name,
        )
        limit = _DOWNLOAD_MAX_BYTES if save_target else context.max_tool_result_bytes
        # A download is bounded by _DOWNLOAD_MAX_BYTES, not by page-load latency;
        # sharing fetch_timeout_s silently demanded ~2 MB/s to move anything large.
        timeout_s = context.download_timeout_s if save_target else context.fetch_timeout_s
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            content_type = _header_value(resp.headers, "content-type")
            if save_target is not None:
                payload, truncated = _read_response(resp, limit)
            else:
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
                        resp, min(limit, _DOWNLOAD_MAX_BYTES)
                    )
                else:
                    payload, too_large = _read_response(resp, _DOWNLOAD_MAX_BYTES)
                    truncated = len(payload) > limit
                    is_transfer = truncated or not _is_text_response(content_type, payload)
                    if is_transfer:
                        warn_urllib_fallback("fetch() response transfer")
                    if too_large and is_transfer:
                        return f"ERROR: response exceeds {_DOWNLOAD_MAX_BYTES} byte download limit"
            response_url = str(getattr(resp, "geturl", lambda: url)() or url)
        if truncated and save_target is not None:
            return f"ERROR: response exceeds {_DOWNLOAD_MAX_BYTES} byte download limit"
        if save_target is not None:
            return _write_download(save_target, payload, content_type, context)
        if aria2c is not None and (
            transfer_from_headers
            or truncated
            or not _is_text_response(content_type, payload)
        ):
            if response_length is not None and response_length > _DOWNLOAD_MAX_BYTES:
                return f"ERROR: response exceeds {_DOWNLOAD_MAX_BYTES} byte download limit"
            return _aria_payload(
                aria2c,
                url,
                headers=normalized_headers,
                content_type=content_type,
                response_url=response_url,
                raw=raw,
                context=context,
            )
        return _format_payload(
            data=payload,
            content_type=content_type,
            raw=raw,
            context=context,
            truncated=truncated,
            base_url=response_url,
        )
    except DownloadError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "shell",
            load_description("shell"),
            shell,
            {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "integer", "default": 300},
                "keep_ansi": {"type": "boolean", "default": False},
                "env": {"type": "array", "items": {"type": "string"}},
                "description": {"type": "string"},
            },
            required=("command",),
        ),
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
        ),
    )
