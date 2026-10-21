"""Helpers for attaching local files to CLI/REPL user turns."""

from __future__ import annotations

import mimetypes
import os
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
from .prompt_text import load_catalog

_PROMPTS = load_catalog('attach')


STDIN_ATTACHMENT_NAME = "<stdin>"
# A name without a suffix, so image detection goes by the magic bytes alone and
# a text file named `fake.png` is not sent as `image/png`.
_DETECT_NAME = Path("attachment")


def _image_mime(header: bytes) -> str | None:
    """The image mime type ``header`` starts with; None when it is no image."""
    mime = _detect_visual_mime(_DETECT_NAME, header)
    return mime if mime and mime.startswith("image/") else None


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


_DOUBLE_QUOTE_ESCAPES = '\\"$`\n'


def _scan_tokens(line: str, *, quotes_everywhere: bool = True, prefix: str = "@") -> list[tuple[str, int, int]]:
    """Split `line` into shell-word tokens, each returned as (unquoted_text, start,
    end) spanning the token's raw location in `line`. Mirrors shlex.split's posix
    quote and backslash handling (so @"a b.png" and @a\\ b.png both name
    `a b.png`), but never raises.

    With `quotes_everywhere` false (the line has an unbalanced quote, as prose
    like "isn't" does), only a token starting with `@` gets quote and escape
    handling; every other token is a plain run of non-space, so a prose
    apostrophe cannot swallow the rest of the line. An `@` token whose own quote
    is left open is then also taken as plain text."""
    tokens: list[tuple[str, int, int]] = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        if not quotes_everywhere and not line.startswith(prefix, i):
            while i < n and not line[i].isspace():
                i += 1
            tokens.append((line[start:i], start, i))
            continue
        word = _scan_word(line, start)
        if word is None:  # open quote inside an @token; only when not quotes_everywhere
            while i < n and not line[i].isspace():
                i += 1
            tokens.append((line[start:i], start, i))
            continue
        text, i = word
        tokens.append((text, start, i))
    return tokens


def _scan_word(line: str, i: int) -> tuple[str, int] | None:
    """One posix shell word starting at `i`: (unquoted text, end). None when a
    quote opened in the word is never closed."""
    n = len(line)
    buf: list[str] = []
    while i < n and not line[i].isspace():
        ch = line[i]
        if ch == "\\":
            if i + 1 < n:
                buf.append(line[i + 1])
                i += 2
            else:
                buf.append(ch)
                i += 1
        elif ch == "'":
            end_quote = line.find("'", i + 1)
            if end_quote == -1:
                return None
            buf.append(line[i + 1:end_quote])
            i = end_quote + 1
        elif ch == '"':
            i += 1
            while i < n and line[i] != '"':
                if line[i] == "\\" and i + 1 < n and line[i + 1] in _DOUBLE_QUOTE_ESCAPES:
                    buf.append(line[i + 1])
                    i += 2
                else:
                    buf.append(line[i])
                    i += 1
            if i >= n:
                return None
            i += 1
        else:
            buf.append(ch)
            i += 1
    return "".join(buf), i


def split_repl_attachments(line: str, *, prefix: str = "@") -> tuple[str, list[str]]:
    """Extract prefixed attachment tokens from a REPL line (default: @path).

    Paths with spaces may be shell-quoted as @"path with spaces.png". If no
    attachment token is present, return the original line unchanged. When one
    or more are found, only their token spans are cut from the original text,
    position-aware — everything else (quotes, repeated spaces, punctuation)
    survives byte-for-byte.

    Each pasted-image placeholder (`[image #N]`, see `js.clipimage`) in the line
    is an attachment too, after the @path ones; the placeholder stays in the
    text so the model can tell which image the words refer to.

    An unbalanced quote (an apostrophe in ordinary prose like "isn't") makes
    shlex raise; that alone must never drop or mangle an attachment, so then
    only the @tokens get quote handling and the rest splits on whitespace.
    """

    if not prefix:
        return line, clipimage.placeholders(line)
    try:
        shlex.split(line)
    except ValueError:
        tokens = _scan_tokens(line, quotes_everywhere=False, prefix=prefix)
    else:
        tokens = _scan_tokens(line, prefix=prefix)

    attachments: list[str] = []
    spans: list[tuple[int, int]] = []
    for text, start, end in tokens:
        if text.startswith(prefix) and text.strip(prefix):
            if text[len(prefix):] not in attachments:
                attachments.append(text[len(prefix):])
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
        raise AttachmentError(msgs.PROMPT_EMPTY.said())

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
            raise AttachmentError(msgs.STDIN_ATTACHMENT_NOT_PIPED.said())
        return _prepare_bytes(STDIN_ATTACHMENT_NAME, Path(STDIN_ATTACHMENT_NAME), stdin_attachment, cfg)
    pasted = clipimage.lookup(raw_path)
    if pasted is not None:
        return _prepare_bytes(raw_path, Path(raw_path), pasted, cfg)

    path = _resolve_path(raw_path, cwd or Path.cwd())
    try:
        stat = path.stat()
    except OSError as exc:
        raise AttachmentError(msgs.ATTACHMENT_NOT_FOUND.said(path=raw_path)) from exc
    if not path.is_file():
        raise AttachmentError(msgs.ATTACHMENT_NOT_A_FILE.said(path=path))
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
    except OSError as exc:
        raise AttachmentError(msgs.ATTACHMENT_UNREADABLE.said(path=path, error=exc)) from exc

    mime = _image_mime(header)
    if mime:
        limit = cfg.max_file_bytes
        if stat.st_size > limit:
            raise AttachmentError(
                msgs.ATTACHMENT_IMAGE_TOO_LARGE.said(path=path, size=stat.st_size, limit=limit))
        try:
            with path.open("rb") as fh:
                data = fh.read(limit + 1)
        except OSError as exc:
            raise AttachmentError(msgs.ATTACHMENT_UNREADABLE.said(path=path, error=exc)) from exc
        if len(data) > limit:  # the file grew after the stat
            raise AttachmentError(
                msgs.ATTACHMENT_IMAGE_TOO_LARGE.said(path=path, size=len(data), limit=limit))
        return _prepare_image(str(path), mime, len(data), data, cfg)

    cap = _text_cap(cfg)
    try:
        with path.open("rb") as fh:
            data = fh.read(cap + 1)
    except OSError as exc:
        raise AttachmentError(msgs.ATTACHMENT_UNREADABLE.said(path=path, error=exc)) from exc
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
    mime = _image_mime(data[:16])
    if mime:
        if size > cfg.max_file_bytes:
            raise AttachmentError(
                msgs.ATTACHMENT_IMAGE_TOO_LARGE.said(path=display_path, size=size, limit=cfg.max_file_bytes))
        return _prepare_image(display_path, mime, size, data, cfg)

    cap = _text_cap(cfg)
    sample = data[: cap + 1]
    if _looks_text(sample):
        truncated = len(sample) > cap or (total_size is not None and total_size > cap)
        kept = _strip_incomplete_utf8_tail(sample[:cap]) if truncated else sample[:cap]
        text = kept.decode("utf-8", errors="replace")
        return _PreparedAttachment(_format_text_attachment(display_path, text, size, truncated, cap))

    guessed, _ = mimetypes.guess_type(str(path))
    file_type = guessed or "application/octet-stream"
    return _PreparedAttachment(
        _PROMPTS['prepare_bytes_attached_binary_file_type_size_bytes'].format(display_path=display_path, file_type=file_type, size=size)
    )


def _prepare_image(display_path: str, mime: str, size: int, data: bytes, cfg: Config) -> _PreparedAttachment:
    stub = _PROMPTS['prepare_image_visual_file_mime_size_bytes'].format(display_path=display_path, mime=mime, size=size)
    if not getattr(cfg, "vision_enabled", False):
        return _PreparedAttachment(_PROMPTS['prepare_image_vision_disabled_image_bytes_not_sent'].format(stub=stub))
    return _PreparedAttachment(stub, ai.types.messages.FilePart(data=data, media_type=mime))


def _resolve_path(raw_path: str, cwd: Path) -> Path:
    path = Path(os.path.expanduser(raw_path))
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def _text_cap(cfg: Config) -> int:
    attachment_cap = int(_settings.knob_attr(cfg, "max_text_attachment_bytes", "ATTACHMENT_MAX"))
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
    if b"\x00" in data:
        return False
    try:
        _strip_incomplete_utf8_tail(data).decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _format_text_attachment(display_path: str, text: str, size: int, truncated: bool, cap: int) -> str:
    fence = _fence_for(text)
    header = _PROMPTS['format_text_attachment_attached_file_bytes'].format(display_path=display_path, size=size)
    if truncated:
        header += _PROMPTS['format_text_attachment_truncated_to_bytes'].format(cap=cap)
    return _PROMPTS['format_text_attachment_text'].format(header=header, fence=fence, text=text, value_3=fence)


def _fence_for(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return fence
