"""Helpers for attaching local files to CLI/REPL user turns."""

from __future__ import annotations

import mimetypes
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable

import ai

from .config import Config
from .toolkit.fs import _detect_visual_mime

TEXT_ATTACHMENT_MAX_BYTES = 64 * 1024
STDIN_ATTACHMENT_NAME = "<stdin>"


class AttachmentError(ValueError):
    """Raised when a requested attachment cannot be prepared."""


@dataclass(frozen=True)
class UserMessageBundle:
    """Provider-facing and history-safe versions of one user message."""

    runtime_message: dict
    history_message: dict


def split_repl_attachments(line: str) -> tuple[str, list[str]]:
    """Extract @path tokens from a REPL line.

    Paths with spaces may be shell-quoted as @"path with spaces.png".
    If no attachment token is present, return the original line unchanged.
    """

    try:
        tokens = shlex.split(line)
    except ValueError:
        return line, []

    attachments: list[str] = []
    prompt_tokens: list[str] = []
    for token in tokens:
        if token.startswith("@") and len(token) > 1:
            attachments.append(token[1:])
        else:
            prompt_tokens.append(token)

    if not attachments:
        return line, []
    return " ".join(prompt_tokens), attachments


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
        raise AttachmentError("prompt is empty")

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
            raise AttachmentError("-f - requires piped stdin attachment bytes")
        return _prepare_bytes(STDIN_ATTACHMENT_NAME, Path(STDIN_ATTACHMENT_NAME), stdin_attachment, cfg)

    path = _resolve_path(raw_path, cwd or Path.cwd())
    try:
        stat = path.stat()
    except OSError as exc:
        raise AttachmentError(f"attachment not found: {raw_path}") from exc
    if not path.is_file():
        raise AttachmentError(f"attachment is not a regular file: {path}")
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
    except OSError as exc:
        raise AttachmentError(f"could not read attachment {path}: {exc}") from exc

    mime = _detect_visual_mime(path, header)
    if mime and mime.startswith("image/"):
        if stat.st_size > getattr(cfg, "max_file_bytes", stat.st_size):
            raise AttachmentError(
                f"image attachment {path} is {stat.st_size} bytes; maximum is {cfg.max_file_bytes} bytes"
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise AttachmentError(f"could not read attachment {path}: {exc}") from exc
        return _prepare_image(str(path), mime, stat.st_size, data, cfg)

    cap = _text_cap(cfg)
    try:
        with path.open("rb") as fh:
            data = fh.read(cap + 1)
    except OSError as exc:
        raise AttachmentError(f"could not read attachment {path}: {exc}") from exc
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
                f"image attachment {display_path} is {size} bytes; maximum is {cfg.max_file_bytes} bytes"
            )
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
    configured = int(getattr(cfg, "max_tool_result_bytes", TEXT_ATTACHMENT_MAX_BYTES) or TEXT_ATTACHMENT_MAX_BYTES)
    return max(1, min(TEXT_ATTACHMENT_MAX_BYTES, configured))


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
