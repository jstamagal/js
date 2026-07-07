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
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .core import Tool, ToolContext
from .descriptions import load_description
from .fs import _detect_visual_mime, _image_marker
from .sanitize import int_or_default, text_or_default


_ENV_ALLOW = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "PWD", "SHELL"}
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TAG_RE = re.compile(r"<[^>]+>")



def _default_shell() -> str:
    if sys.platform == "win32":
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL", "/bin/sh")


def _drain_capped(stream, cap: int) -> bytes:
    """Read a pipe to EOF keeping at most ``cap`` bytes.

    subprocess.run(capture_output=True) buffers EVERYTHING before any cap is
    applied — one `yes`-style command took a js process to 92 GB RSS and the
    OOM killer. Past the cap we keep draining (so the child never blocks on a
    full pipe) but discard, holding memory at the cap.
    """
    kept = bytearray()
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return bytes(kept)
        if len(kept) < cap:
            kept.extend(chunk[: cap - len(kept)])


def _run_capped(
    argv: list[str], *, timeout: int, cwd: str, env: dict[str, str], cap: int
) -> tuple[int, bytes, bytes]:
    """Run ``argv`` capturing at most ``cap`` bytes per stream.

    Raises subprocess.TimeoutExpired like subprocess.run; the child is killed
    on timeout either way.
    """
    import threading

    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env
    )
    out: dict[str, bytes] = {"stdout": b"", "stderr": b""}

    def _reader(name: str, stream) -> None:
        try:
            out[name] = _drain_capped(stream, cap)
        finally:
            stream.close()

    threads = [
        threading.Thread(target=_reader, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=_reader, args=("stderr", proc.stderr), daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    for t in threads:
        t.join(timeout=10)
    return rc, out["stdout"], out["stderr"]


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
    allowed = set(env or []) | _ENV_ALLOW
    safe_env = {key: os.environ[key] for key in allowed if key in os.environ}
    shell_path = _default_shell()
    shell_arg = "/C" if sys.platform == "win32" else "-c"
    try:
        returncode, raw_stdout, raw_stderr = _run_capped(
            [shell_path, shell_arg, command],
            timeout=timeout,
            cwd=str(workdir),
            env=safe_env,
            cap=context.max_bash_output_bytes,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except OSError as exc:
        return f"ERROR: {exc}"

    stdout = raw_stdout.decode("utf-8", errors="replace")
    stderr = raw_stderr.decode("utf-8", errors="replace")
    if not keep_ansi:
        stdout = _ANSI_RE.sub("", stdout)
        stderr = _ANSI_RE.sub("", stderr)
    parts = [f"shell={shell_path}", f"exit={returncode}"]
    if description:
        parts.append(f"description={description}")
    if stdout:
        parts.append(f"--- stdout ---\n{stdout}")
    if stderr:
        parts.append(f"--- stderr ---\n{stderr}")
    if not stdout and not stderr:
        parts.append("(no output)")
    return "\n".join(parts)


def _html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
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

    payload = data[: context.max_tool_result_bytes]
    text = payload.decode("utf-8", errors="replace")
    if not raw and "html" in _media_type(content_type):
        text = _html_to_text(text)
    if len(text) > context.max_tool_result_bytes:
        text = text[: context.max_tool_result_bytes]
        truncated = True
    if truncated:
        text += "\n[truncated]"
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

        with path.open("rb") as handle:
            data = handle.read(context.max_tool_result_bytes + 1)
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

        req = urllib.request.Request(
            url,
            data=data,
            headers=normalized_headers,
            method=method_name,
        )
        limit = _DOWNLOAD_MAX_BYTES if save_target else context.max_tool_result_bytes
        with urllib.request.urlopen(req, timeout=context.fetch_timeout_s) as resp:
            content_type = _header_value(resp.headers, "content-type")
            payload, truncated = _read_response(resp, limit)
        if truncated and save_target is not None:
            return f"ERROR: response exceeds {_DOWNLOAD_MAX_BYTES} byte download limit"
        if save_target is not None:
            return _write_download(save_target, payload, content_type, context)
        return _format_payload(
            data=payload,
            content_type=content_type,
            raw=raw,
            context=context,
            truncated=truncated,
        )
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
