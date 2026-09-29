"""Helpers for attaching local files to CLI/REPL user turns."""

from __future__ import annotations

import mimetypes
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable

import ai

from . import clipimage
from . import messages as msgs
from . import settings as _settings
from .config import Config
from .toolkit.fs import _detect_visual_mime

STDIN_ATTACHMENT_NAME = "<stdin>"


class AttachmentError(ValueError):
    """Raised when a requested attachment cannot be prepared."""


@dataclass(frozen=True)
class UserMessageBundle:
    """Provider-facing and history-safe versions of one user message."""

    runtime_message: dict
    history_message: dict


def with_note(bundle: UserMessageBundle, note: str) -> UserMessageBundle:
    """``bundle`` with ``note`` appended to its text, in both versions."""
    history_text = bundle.history_message.get("content") or ""
    text = f"{history_text}\n\n{note}" if history_text else note
    runtime_content = bundle.runtime_message.get("content")
    if isinstance(runtime_content, list):
        parts = list(runtime_content)
        if parts and isinstance(parts[0], ai.types.messages.TextPart):
            parts[0] = ai.types.messages.TextPart(text=text)
        else:
            parts.insert(0, ai.types.messages.TextPart(text=text))
        runtime_content = parts
    else:
        runtime_content = text
    return UserMessageBundle(
        runtime_message={**bundle.runtime_message, "content": runtime_content},
        history_message={**bundle.history_message, "content": text},
    )


def _scan_tokens(line: str) -> list[tuple[str, int, int]]:
    """Split `line` into shell-word tokens, each returned as (unquoted_text, start,
    end) spanning the token's raw location in `line`. Mirrors shlex.split's posix
    quote handling closely enough that a quote may be embedded mid-token (e.g.
    @"a b.png"), but — unlike shlex — never raises: an unterminated quote just
    runs to end of string. Only called once shlex.split has already accepted the
    line, so quotes are known to be balanced and this matches its result."""
    tokens: list[tuple[str, int, int]] = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        buf: list[str] = []
        while i < n and not line[i].isspace():
            ch = line[i]
            if ch in "'\"":
                i += 1
                q_start = i
                end_quote = line.find(ch, i)
                if end_quote == -1:
                    buf.append(line[q_start:n])
                    i = n
                else:
                    buf.append(line[q_start:end_quote])
                    i = end_quote + 1
            else:
                buf.append(ch)
                i += 1
        tokens.append(("".join(buf), start, i))
    return tokens


def split_repl_attachments(line: str) -> tuple[str, list[str]]:
    """Extract @path tokens from a REPL line.

    Paths with spaces may be shell-quoted as @"path with spaces.png". If no
    attachment token is present, return the original line unchanged. When one
    or more are found, only their token spans are cut from the original text,
    position-aware — everything else (quotes, repeated spaces, punctuation)
    survives byte-for-byte.

    Each pasted-image placeholder (`[image #N]`, see `js.clipimage`) in the line
    is an attachment too, after the @path ones; the placeholder stays in the
    text so the model can tell which image the words refer to.

    An unbalanced quote (an apostrophe in ordinary prose like "isn't") makes
    shlex raise; that alone must never drop an attachment, so this falls back
    to a plain whitespace split for token/span extraction in that case.
    """

    try:
        shlex.split(line)
    except ValueError:
        tokens = [(m.group(), m.start(), m.end()) for m in re.finditer(r"\S+", line)]
    else:
        tokens = _scan_tokens(line)

    attachments: list[str] = []
    spans: list[tuple[int, int]] = []
    for text, start, end in tokens:
        if text.startswith("@") and text.strip("@"):
            attachments.append(text[1:])
            spans.append((start, end))

    prompt = line
    for start, end in sorted(spans, reverse=True):
        prompt = prompt[:start] + prompt[end:]
    return prompt, attachments + clipimage.placeholders(prompt)


def build_user_message(
    prompt: str,
    attachments: Iterable[str] | None,
    cfg: Config,
    *,
    cwd: Path | None = None,
    stdin_attachment: bytes | None = None,
) -> UserMessageBundle:
    """Build one user message with optional file attachments.

    ``runtime_message`` may contain ``ai`` parts for the current provider call.
    ``history_message`` is always JSONL-lightweight text.
    """

    paths = list(attachments or [])
    text_blocks: list[str] = []
    file_parts: list[ai.types.messages.FilePart] = []
    if prompt:
        text_blocks.append(prompt)

    for raw_path in paths:
        prepared = _prepare_attachment(
            raw_path,
            cfg,
            cwd=cwd,
            stdin_attachment=stdin_attachment if raw_path == "-" else None,
        )
        text_blocks.append(prepared.text)
        if prepared.file_part is not None:
            file_parts.append(prepared.file_part)

    text = "\n\n".join(block for block in text_blocks if block)
    if not text and not file_parts:
        raise AttachmentError(msgs.PROMPT_EMPTY.text())

    if file_parts:
        content: list[object] = []
        if text:
            content.append(ai.types.messages.TextPart(text=text))
        content.extend(file_parts)
        runtime_content: str | list[object] = content
    else:
        runtime_content = text

    history_message = {"role": "user", "content": text}
    runtime_message = {"role": "user", "content": runtime_content}
    return UserMessageBundle(runtime_message=runtime_message, history_message=history_message)


@dataclass(frozen=True)
class _PreparedAttachment:
    text: str
    file_part: ai.types.messages.FilePart | None = None


def _prepare_attachment(
    raw_path: str,
    cfg: Config,
    *,
    cwd: Path | None,
    stdin_attachment: bytes | None,
) -> _PreparedAttachment:
    if raw_path == "-":
        if stdin_attachment is None:
            raise AttachmentError(msgs.STDIN_ATTACHMENT_NOT_PIPED.text())
        return _prepare_bytes(STDIN_ATTACHMENT_NAME, Path(STDIN_ATTACHMENT_NAME), stdin_attachment, cfg)
    pasted = clipimage.lookup(raw_path)
    if pasted is not None:
        return _prepare_bytes(raw_path, Path(raw_path), pasted, cfg)

    path = _resolve_path(raw_path, cwd or Path.cwd())
    try:
        stat = path.stat()
    except OSError as exc:
        raise AttachmentError(msgs.ATTACHMENT_NOT_FOUND.text(path=raw_path)) from exc
    if not path.is_file():
        raise AttachmentError(msgs.ATTACHMENT_NOT_A_FILE.text(path=path))
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
    except OSError as exc:
        raise AttachmentError(msgs.ATTACHMENT_UNREADABLE.text(path=path, error=exc)) from exc

    mime = _detect_visual_mime(path, header)
    if mime and mime.startswith("image/"):
        if stat.st_size > getattr(cfg, "max_file_bytes", stat.st_size):
            raise AttachmentError(
                msgs.ATTACHMENT_IMAGE_TOO_LARGE.text(path=path, size=stat.st_size, limit=cfg.max_file_bytes))
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise AttachmentError(msgs.ATTACHMENT_UNREADABLE.text(path=path, error=exc)) from exc
        return _prepare_image(str(path), mime, stat.st_size, data, cfg)

    cap = _text_cap(cfg)
    try:
        with path.open("rb") as fh:
            data = fh.read(cap + 1)
    except OSError as exc:
        raise AttachmentError(msgs.ATTACHMENT_UNREADABLE.text(path=path, error=exc)) from exc
    return _prepare_bytes(str(path), path, data, cfg, total_size=stat.st_size)


def _prepare_bytes(
    display_path: str,
    path: Path,
    data: bytes,
    cfg: Config,
    *,
    total_size: int | None = None,
) -> _PreparedAttachment:
    size = len(data) if total_size is None else total_size
    mime = _detect_visual_mime(path, data[:16])
    if mime and mime.startswith("image/"):
        if size > getattr(cfg, "max_file_bytes", size):
            raise AttachmentError(
                msgs.ATTACHMENT_IMAGE_TOO_LARGE.text(path=display_path, size=size, limit=cfg.max_file_bytes))
        return _prepare_image(display_path, mime, size, data, cfg)

    cap = _text_cap(cfg)
    sample = data[: cap + 1]
    if _looks_text(sample):
        truncated = len(sample) > cap or (total_size is not None and total_size > cap)
        text = sample[:cap].decode("utf-8", errors="replace")
        return _PreparedAttachment(_format_text_attachment(display_path, text, size, truncated, cap))

    guessed, _ = mimetypes.guess_type(str(path))
    file_type = guessed or "application/octet-stream"
    return _PreparedAttachment(
        f"ATTACHED_BINARY_FILE {display_path} type={file_type} size={size} bytes (content not inlined)"
    )


def _prepare_image(display_path: str, mime: str, size: int, data: bytes, cfg: Config) -> _PreparedAttachment:
    stub = f"VISUAL_FILE {display_path} mime={mime} size={size} bytes"
    if not getattr(cfg, "vision_enabled", False):
        return _PreparedAttachment(f"{stub} (vision disabled; image bytes not sent)")
    return _PreparedAttachment(stub, ai.types.messages.FilePart(data=data, media_type=mime))


def _resolve_path(raw_path: str, cwd: Path) -> Path:
    path = Path(os.path.expanduser(raw_path))
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def _text_cap(cfg: Config) -> int:
    attachment_cap = int(_settings.knob(getattr(cfg, "settings", None), "limits.max_text_attachment_bytes"))
    configured = int(getattr(cfg, "max_tool_result_bytes", attachment_cap) or attachment_cap)
    return max(1, min(attachment_cap, configured))


def _strip_incomplete_utf8_tail(data: bytes) -> bytes:
    """Drop a partial multibyte UTF-8 sequence left dangling at the end of a
    byte-limited read, so a codepoint split across the read boundary doesn't make
    valid UTF-8 look like binary."""
    for back in range(1, 4):
        if back > len(data):
            break
        b = data[-back]
        if b < 0x80:            # plain ascii tail byte, nothing in progress
            break
        if b >= 0xC0:           # lead byte: it starts an N-byte sequence
            need = 4 if b >= 0xF0 else 3 if b >= 0xE0 else 2
            return data[: -back] if back < need else data
    return data


def _looks_text(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return False
    try:
        _strip_incomplete_utf8_tail(data).decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _format_text_attachment(display_path: str, text: str, size: int, truncated: bool, cap: int) -> str:
    fence = _fence_for(text)
    header = f"Attached file: {display_path} ({size} bytes)"
    if truncated:
        header += f" [truncated to {cap} bytes]"
    return f"{header}\n{fence}\n{text}\n{fence}"


def _fence_for(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return fence
