"""Shell and network tools."""

from __future__ import annotations

import atexit
import html
import json
import mimetypes
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import settings as _settings
from ..capped_process import (
    CappedProcess,
    CappedProcessResult,
    start_capped,
    truncation_marker,
)
from ..tool_binaries import (
    ARIA2_EXECUTABLE,
    DownloadError,
    download_with_aria2,
    TOOLS_DIR,
    resolve_binary,
    warn_urllib_fallback,
)
from .core import Tool, ToolContext
from .descriptions import load_description
from .fs import _detect_visual_mime, _image_marker, _read_regular_bytes
from .sanitize import int_or_default, text_or_default
from .search import _absolutize


_ENV_ALLOW = _settings.DEFAULT_SHELL_ENV_ALLOW
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_RE = re.compile(r"(?is)<a\b(?P<attrs>[^>]*)>(?P<label>.*?)</a\s*>")
_HREF_RE = re.compile(
    r"(?is)(?:^|\s)href\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))"
)



def _default_shell() -> str:
    if sys.platform == "win32":
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL", "/bin/sh")


# Commands the shell tool started that had not exited when their call returned.
# Keyed by handle id; one table per process, shared across turns so a poll in a
# later turn finds the job a previous turn started.
_JOBS: dict[str, _ShellJob] = {}
_JOB_SEQ = 0
_JOBS_LOCK = threading.Lock()
KEEP_FINISHED_JOBS = 5


class _ShellJob:
    def __init__(self, job_id: str, command: str, process: CappedProcess,
                 shell_path: str, keep_ansi: bool, cap: int) -> None:
        self.id = job_id
        self.command = command
        self.process = process
        self.shell_path = shell_path
        self.keep_ansi = keep_ansi
        self.cap = cap
        self.delivered = (0, 0)     # bytes of (stdout, stderr) already handed to the model

    def running(self) -> bool:
        return self.process.running()


def _register_job(job: _ShellJob) -> None:
    with _JOBS_LOCK:
        _JOBS[job.id] = job
        finished = [j for j in _JOBS.values() if not j.running()]
        for stale in finished[:-KEEP_FINISHED_JOBS] if len(finished) > KEEP_FINISHED_JOBS else []:
            _JOBS.pop(stale.id, None)


def _next_job_id() -> str:
    global _JOB_SEQ
    with _JOBS_LOCK:
        _JOB_SEQ += 1
        return str(_JOB_SEQ)


def _find_job(handle: str | None) -> _ShellJob | None:
    with _JOBS_LOCK:
        if handle:
            return _JOBS.get(str(handle))
        running = [j for j in _JOBS.values() if j.running()]
        if running:
            return running[-1]
        return next(reversed(_JOBS.values()), None) if _JOBS else None


@atexit.register
def _kill_live_jobs() -> None:
    for job in list(_JOBS.values()):
        if job.running():
            job.process.kill()


def _clean(raw: bytes, keep_ansi: bool) -> str:
    text = raw.decode("utf-8", errors="replace")
    return text if keep_ansi else _ANSI_RE.sub("", text)


def _job_new_output(job: _ShellJob) -> tuple[str, str]:
    """Output produced since the last time this job was reported."""
    out, err = job.process.snapshot()
    seen_out, seen_err = job.delivered
    job.delivered = (len(out), len(err))
    return _clean(out[seen_out:], job.keep_ansi), _clean(err[seen_err:], job.keep_ansi)


def _render_finished(job: _ShellJob, result: CappedProcessResult, description: str | None,
                     allowed: set[str], safe_env: dict[str, str], *, since_last: bool) -> str:
    if since_last:
        stdout, stderr = _job_new_output(job)
    else:
        stdout = _clean(result.stdout, job.keep_ansi)
        stderr = _clean(result.stderr, job.keep_ansi)
        job.delivered = (len(result.stdout), len(result.stderr))
    marker = truncation_marker(job.cap)
    if result.stdout_truncated:
        stdout = f"{stdout}\n{marker}" if stdout else marker
    if result.stderr_truncated:
        stderr = f"{stderr}\n{marker}" if stderr else marker
    parts = [f"shell={job.shell_path}", f"exit={result.returncode}"]
    if description:
        parts.append(f"description={description}")
    if result.returncode:
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


def _render_running(job: _ShellJob, waited: float) -> str:
    stdout, stderr = _job_new_output(job)
    parts = [
        f"command still running after {waited:.0f}s (handle {job.id}, pid {job.process.pid}). "
        f"It keeps running. Poll it with action=\"poll\", handle=\"{job.id}\" for new output, "
        f"action=\"wait\", handle=\"{job.id}\", timeout=N to block for it, "
        f"or action=\"kill\", handle=\"{job.id}\" to stop it.",
    ]
    if stdout:
        parts.append(f"--- stdout so far ---\n{stdout}")
    if stderr:
        parts.append(f"--- stderr so far ---\n{stderr}")
    parts.append(f"HANDLE {job.id} RUNNING")
    return "\n".join(parts)


def shell(
    command: str = "",
    cwd: str | None = None,
    timeout: int | None = None,
    keep_ansi: bool = False,
    env: list[str] | None = None,
    description: str | None = None,
    action: str = "run",
    handle: str | None = None,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    action = (text_or_default(action, "run") or "run").strip().lower()
    if action in ("poll", "wait", "kill"):
        return _shell_job_action(action, handle, timeout, description)
    if action != "run":
        return f"ERROR: unknown action {action!r}; expected run, poll, wait, or kill"
    command = text_or_default(command)
    if not command.strip():
        return "ERROR: command is required for action=\"run\""
    wait_s = int_or_default(timeout, int(getattr(context, "shell_wait_seconds", _settings.DEFAULT_SHELL_WAIT_SECONDS)), minimum=1)
    workdir = context.resolve_path(cwd) if cwd else context.cwd
    configured_allow = getattr(context, "shell_env_allow", _ENV_ALLOW)
    if not isinstance(configured_allow, (list, tuple, set, frozenset)):
        configured_allow = _ENV_ALLOW
    allowed = {str(key) for key in configured_allow if str(key)} | set(env or [])
    safe_env = {key: os.environ[key] for key in allowed if key in os.environ}
    # The managed binaries are downloaded for a command to call by name, so the
    # directory holding them leads PATH. Without this fd, bat and fzf are
    # installed and unreachable.
    if "PATH" in allowed:
        managed_bin = str(TOOLS_DIR)
        inherited = safe_env.get("PATH", "")
        safe_env["PATH"] = f"{managed_bin}{os.pathsep}{inherited}" if inherited else managed_bin
    shell_path = _default_shell()
    shell_arg = "/C" if sys.platform == "win32" else "-c"
    cap = int(context.max_bash_output_bytes)
    ceiling = int(getattr(context, "max_bash_output_ceiling", 0) or 0)
    if ceiling > 0:
        cap = min(cap, ceiling)
    # A command can create/edit/delete anything, so memoized fs_search results are
    # no longer trustworthy once one has run.
    context.invalidate_search_cache()
    try:
        process = start_capped(
            [shell_path, shell_arg, command],
            cwd=str(workdir),
            env=safe_env,
            cap=cap,
        )
    except OSError as exc:
        return f"ERROR: {exc}"
    job = _ShellJob(_next_job_id(), command, process, shell_path, keep_ansi, cap)
    job.allowed, job.safe_env = allowed, safe_env
    _register_job(job)
    result = process.wait(wait_s)
    if result is None:
        # The command outlived the window. It is NOT killed: a long build or
        # test run finishing on its own beats one killed at an arbitrary
        # deadline, and the model gets a handle to come back to it instead of
        # the operator sitting through the wait.
        return _render_running(job, process.elapsed())
    return _render_finished(job, result, description, allowed, safe_env, since_last=False)


def _shell_job_action(action: str, handle: str | None, timeout: int | None, description: str | None) -> str:
    job = _find_job(handle)
    if job is None:
        return f"ERROR: no shell job{' ' + str(handle) if handle else ''} to {action}"
    if action == "kill":
        was_running = job.running()
        result = job.process.kill()
        if not was_running:
            return f"handle {job.id} had already exited\n" + _render_finished(
                job, result, description, job.allowed, job.safe_env, since_last=True)
        return f"killed handle {job.id} after {job.process.elapsed():.0f}s\n" + _render_finished(
            job, result, description, job.allowed, job.safe_env, since_last=True)
    wait_s = 0 if action == "poll" else int_or_default(timeout, _settings.DEFAULT_SHELL_WAIT_SECONDS, minimum=1)
    result = job.process.wait(wait_s)
    if result is None:
        return _render_running(job, job.process.elapsed())
    return _render_finished(job, result, description, job.allowed, job.safe_env, since_last=True)


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


# What may land in RAM for an INLINE result. A saved download is a different
# question entirely: it streams to disk and is bounded by limits.max_download_bytes,
# which defaults to unlimited. A 6GB ISO is a normal thing to fetch(save=...).
_DOWNLOAD_MAX_BYTES = 32 * 1024 * 1024
_STREAM_CHUNK = 1024 * 1024
_INLINE_READ_CHUNK = 64 * 1024


class _FetchTimeoutError(Exception):
    pass


def _inline_read_limit_error() -> str:
    return (
        f"ERROR: response exceeds {_DOWNLOAD_MAX_BYTES} byte inline read limit; "
        "use save= to stream the full response to disk"
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
) -> str:
    """Move bytes to disk without ever holding the whole transfer in memory.

    Writes to a hidden sibling and publishes with os.replace, so an interrupted
    or over-limit transfer never leaves a half-file at the name the caller asked
    for. This is what lets max_download_bytes default to unlimited.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.parent / f".{target.name}.js-partial"
    written = 0
    try:
        with partial.open("wb") as out:
            while True:
                chunk = read_chunk(_STREAM_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if limit is not None and written > limit:
                    raise DownloadError(f"response exceeds {limit} byte download limit")
                out.write(chunk)
    except DownloadError as exc:
        partial.unlink(missing_ok=True)
        return f"ERROR: {exc}"
    except OSError as exc:
        partial.unlink(missing_ok=True)
        return f"ERROR: {type(exc).__name__}: {exc}"
    context.snapshot(target)
    os.replace(partial, target)
    return (
        f"SAVED_RESPONSE path={target} size={written} bytes "
        f"content-type={content_type or 'unknown'}"
    )
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
    path = Path(urllib.request.url2pathname(parsed.path))
    try:
        size = path.stat().st_size
        if save_target is not None:
            # Copying a local file to another path is a transfer, not a read: it
            # streams, so the size of the thing is the caller's business. The
            # head read doubles as the non-regular-file guard.
            head = _read_regular_bytes(path, 4096)
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
            return f"ERROR: file exceeds {_DOWNLOAD_MAX_BYTES} byte read limit; use save= or fs_read"
        data = _read_regular_bytes(path, _DOWNLOAD_MAX_BYTES + 1)
        if len(data) > _DOWNLOAD_MAX_BYTES:
            return f"ERROR: file exceeds {_DOWNLOAD_MAX_BYTES} byte read limit; use save= or fs_read"
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
                    resp.read, save_target, content_type, context, _download_limit(context)
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
                    warn_urllib_fallback("fetch() response transfer")
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


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "shell",
            load_description("shell"),
            shell,
            {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {
                    "type": "integer",
                    "description": "Seconds this call blocks before returning a handle. The command is never killed by this.",
                },
                "action": {"type": "string", "enum": ["run", "poll", "wait", "kill"], "default": "run"},
                "handle": {"type": "string", "description": "Job id from a HANDLE line, for poll/wait/kill."},
                "keep_ansi": {"type": "boolean", "default": False},
                "env": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Variable names to pass through. The child environment is filtered by default.",
                },
                "description": {"type": "string"},
            },
            required=(),
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
