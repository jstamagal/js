"""Pi's read, edit and write tools for the js tool runtime."""

# Adapted from packages/coding-agent/src/core/tools in
# https://github.com/earendil-works/pi at 8209929fb553e7a8c91dc0894569f9ec43dd201f.
# MIT License
#
# Copyright (c) 2025 Mario Zechner
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
from __future__ import annotations

import codecs
import errno
import io
import json
import os
import re
import shlex
import stat
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import unquote, urlsplit

from .. import imagefit
from ..media import IMAGE_RESULT_PREFIX
from . import file_bytes
from .core import Output, Tool, ToolContext, ToolResult
from .descriptions import load_description, load_parameter_description
from .presentation import Presentation

_MAX_LINES = 2000
_MAX_BYTES = 50 * 1024
_MUTATION_LOCKS = tuple(Lock() for _ in range(64))
_PATH_SPACES = str.maketrans(dict.fromkeys("\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000", " "))
_FUZZY_PUNCTUATION = {
    **dict.fromkeys(map(ord, "‘’‚‛"), "'"),
    **dict.fromkeys(map(ord, '“”„‟'), '"'),
    **dict.fromkeys(map(ord, "‐‑‒–—―−"), "-"),
    **dict.fromkeys(map(ord, "\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"), " "),
}
_JS_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def _path(path: str, context: ToolContext, *, write: bool = False, read_variants: bool = False) -> Path:
    raw = path.translate(_PATH_SPACES).removeprefix("@")
    if raw.startswith("file://"):
        url = urlsplit(raw)
        if url.netloc not in ("", "localhost"):
            raise ValueError(f"Invalid file URL: {path}")
        raw = unquote(url.path)
    absolute = context.resolve_path(raw, write=write)
    if read_variants and not absolute.exists():
        nfd = unicodedata.normalize("NFD", raw)
        variants = (
            re.sub(r" (AM|PM)\.", lambda match: "\u202f" + match[1] + ".", raw, flags=re.IGNORECASE),
            nfd,
            raw.replace("'", "’"),
            nfd.replace("'", "’"),
        )
        for variant in variants:
            if variant != raw:
                candidate = context.resolve_path(variant)
                if candidate.exists():
                    return candidate
    return absolute


def _size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024**2:
        return f"{size / 1024:.1f}KB"
    return f"{size / 1024**2:.1f}MB"


@dataclass
class _ReadHead:
    lines: list[str]
    size: int = 0
    first_line_size: int = 0
    truncated_by: str = ""

    def add(self, line: str, size: int, *, terminal_empty: bool = False) -> None:
        if self.truncated_by:
            return
        if not self.lines:
            self.first_line_size = size
        size += bool(self.lines)
        if len(self.lines) >= _MAX_LINES and not terminal_empty:
            self.truncated_by = "lines"
        elif self.size + size > _MAX_BYTES:
            self.truncated_by = "lines" if len(self.lines) >= _MAX_LINES else "bytes"
        else:
            self.lines.append(line)
            self.size += size


def _select_lines(target: Path, start: float, limit: float | None) -> tuple[_ReadHead, int]:
    head = _ReadHead([])
    end = None if limit is None else start + limit
    if start != int(start) or (limit is not None and (limit < 0 or limit != int(limit))):
        lines = file_bytes.read_regular_bytes(target).decode("utf-8", errors="replace").split("\n")
        selected = lines[int(start):None if end is None else int(end)]
        for index, line in enumerate(selected):
            head.add(line, len(line.encode()), terminal_empty=not line and index == len(selected) - 1)
        return head, len(lines)

    # Count the entire file but retain just the lines that could fit in the output.
    fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"not a regular file: {target}")
        remaining = info.st_size or None
        index = 0
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pieces: list[str] = []
        size = 0
        while remaining is None or remaining:
            chunk = stream.readline(65536 if remaining is None else min(remaining, 65536))
            if not chunk:
                break
            if remaining is not None:
                remaining -= len(chunk)
            newline = chunk.endswith(b"\n")
            if index >= start and (end is None or index < end) and not head.truncated_by:
                piece = decoder.decode(chunk.removesuffix(b"\n") if newline else chunk, final=newline)
                size += len(piece.encode())
                if size <= _MAX_BYTES:
                    pieces.append(piece)
                if newline:
                    head.add("".join(pieces), size, terminal_empty=size == 0 and end == index + 1)
                    decoder.reset()
                    pieces.clear()
                    size = 0
            if newline:
                index += 1
        if index >= start and (end is None or index < end) and not head.truncated_by:
            tail = decoder.decode(b"", final=True)
            head.add("".join(pieces) + tail, size + len(tail.encode()), terminal_empty=not pieces and not tail)
    return head, index + 1


def _image_mime(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff") and header[3:4] != b"\xf7":
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"):
        position = 8
        while position + 8 <= len(header):
            kind = header[position + 4:position + 8]
            if kind == b"acTL":
                return None
            if kind == b"IDAT":
                break
            position += 12 + int.from_bytes(header[position:position + 4], "big")
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"BM") and len(header) >= 26:
        file_size = int.from_bytes(header[2:6], "little")
        pixel_offset = int.from_bytes(header[10:14], "little")
        dib_size = int.from_bytes(header[14:18], "little")
        color = header[22:26] if dib_size == 12 else header[26:30]
        if (dib_size == 12 or 40 <= dib_size <= 124) and pixel_offset >= 14 + dib_size:
            if (not file_size or pixel_offset < file_size) and len(color) == 4:
                if color[:2] == b"\x01\x00" and int.from_bytes(color[2:], "little") in (1, 4, 8, 16, 24, 32):
                    return "image/bmp"
    return None


def _read_image(target: Path, mime: str, context: ToolContext) -> Output | ToolResult:
    if not context.vision_enabled:
        return ToolResult.text(f"Read image file [{mime}]\n[Image omitted: the selected model does not support image input.]")
    data = file_bytes.read_regular_bytes(target)
    if mime == "image/bmp":
        try:
            from PIL import Image
        except ImportError:
            return ToolResult.text(f"Read image file [{mime}]\n[Image omitted: could not be converted to a supported inline image format.]")
        try:
            with Image.open(io.BytesIO(data)) as image:
                converted = io.BytesIO()
                image.save(converted, format="PNG")
                data = converted.getvalue()
        except (OSError, ValueError, Image.DecompressionBombError):
            return ToolResult.text(f"Read image file [{mime}]\n[Image omitted: could not be converted to a supported inline image format.]")
    fitted = imagefit.fit(data, "image/png" if mime == "image/bmp" else mime)
    if fitted is None:
        if mime != "image/bmp" and len(data) <= imagefit.MAX_BYTES and "\t" not in str(target):
            return Output(f"{IMAGE_RESULT_PREFIX}{target}\t{mime}\tRead image file [{mime}]")
        return ToolResult.text(f"Read image file [{mime}]\n[Image omitted: could not be resized below the inline image size limit.]")
    notes = [f"Read image file [{fitted.mime}]"]
    if fitted.mime != mime and mime == "image/bmp":
        notes.append(f"[Image converted from {mime} to {fitted.mime}.]")
    if fitted.note:
        notes.append(fitted.note)
    return Output(f"{IMAGE_RESULT_PREFIX}{imagefit.stored(fitted)}\t{fitted.mime}\t" + "\n".join(notes))


def pi_read(path: str, offset: float | None = None, limit: float | None = None, *,
            context: ToolContext) -> str | ToolResult:
    try:
        target = _path(path, context, read_variants=True)
        mime = _image_mime(file_bytes.read_regular_bytes(target, 4100))
        if mime:
            return _read_image(target, mime, context)
        start = max(0, (offset or 1) - 1)
        head, total = _select_lines(target, start, limit)
    except (OSError, ValueError) as exc:
        return f"ERROR: {exc}"
    if start >= total:
        return f"ERROR: Offset {offset:g} is beyond end of file ({total} lines total)"
    first = start + 1
    if head.first_line_size > _MAX_BYTES:
        return ToolResult.text(f"[Line {first:g} is {_size(head.first_line_size)}, exceeds {_size(_MAX_BYTES)} limit. "
                      f"Use a shell: sed -n '{first:g}p' {shlex.quote(path)} | head -c {_MAX_BYTES}]")
    content = "\n".join(head.lines)
    if head.truncated_by:
        end = start + len(head.lines)
        cap = f" ({_size(_MAX_BYTES)} limit)" if head.truncated_by == "bytes" else ""
        return ToolResult.text(f"{content}\n\n[Showing lines {first:g}-{end:g} of {total}{cap}. Use offset={end + 1:g} to continue.]")
    end = total if limit is None else min(start + limit, total)
    if end < total:
        return ToolResult.text(f"{content}\n\n[{total - end:g} more lines in file. Use offset={end + 1:g} to continue.]")
    return ToolResult.text(content)


def _fuzzy(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return "\n".join(line.rstrip(_JS_WHITESPACE) for line in normalized.split("\n")).translate(_FUZZY_PUNCTUATION)


@dataclass(frozen=True)
class _Replacement:
    start: int
    end: int
    text: str


def _splice(content: str, changes: list[_Replacement]) -> str:
    parts = []
    position = 0
    for change in changes:
        parts.extend((content[position:change.start], change.text))
        position = change.end
    parts.append(content[position:])
    return "".join(parts)


def _widen_fuzzy(original: str, fuzzy: str, changes: list[_Replacement]) -> list[_Replacement]:
    original_starts = [0, *(m.end() for m in re.finditer("\n", original))]
    fuzzy_starts = [0, *(m.end() for m in re.finditer("\n", fuzzy))]
    groups: list[tuple[int, int, list[_Replacement]]] = []
    for change in changes:
        first = bisect_right(fuzzy_starts, change.start) - 1
        last = bisect_right(fuzzy_starts, max(change.start, change.end - 1)) - 1
        if groups and first <= groups[-1][1]:
            first, previous_last, grouped = groups.pop()
            groups.append((first, max(last, previous_last), [*grouped, change]))
        else:
            groups.append((first, last, [change]))
    replacements = []
    for first, last, grouped in groups:
        start = fuzzy_starts[first]
        end = fuzzy_starts[last + 1] if last + 1 < len(fuzzy_starts) else len(fuzzy)
        original_end = original_starts[last + 1] if last + 1 < len(original_starts) else len(original)
        local = [_Replacement(change.start - start, change.end - start, change.text) for change in grouped]
        replacements.append(_Replacement(original_starts[first], original_end, _splice(fuzzy[start:end], local)))
    return replacements


def _match_edits(content: str, edits: list[dict[str, str]], path: str) -> list[_Replacement]:
    normalized = [(file_bytes.normalize_line_endings(edit["oldText"], "\n"),
                   file_bytes.normalize_line_endings(edit["newText"], "\n")) for edit in edits]
    single = len(edits) == 1
    for index, (old, _) in enumerate(normalized):
        if not old:
            label = "oldText" if single else f"edits[{index}].oldText"
            raise ValueError(f"{label} must not be empty in {path}.")

    fuzzy = _fuzzy(content)
    use_fuzzy = any(old not in content and _fuzzy(old) in fuzzy for old, _ in normalized)
    base = fuzzy if use_fuzzy else content
    matched = []
    for index, (old, new) in enumerate(normalized):
        fuzzy_old = _fuzzy(old)
        needle = old if old in base else fuzzy_old
        source = base if old in base else fuzzy
        start = source.find(needle)
        if start < 0:
            label = "the exact text" if single else f"edits[{index}]"
            old_label = "old text" if single else "oldText"
            raise ValueError(f"Could not find {label} in {path}. The {old_label} must match exactly including all whitespace and newlines.")
        count = fuzzy.count(fuzzy_old) if fuzzy_old else max(0, len(fuzzy.encode("utf-16-le")) // 2 - 1)
        if count > 1:
            label = "the text" if single else f"edits[{index}]"
            subject = "The text" if single else "Each oldText"
            raise ValueError(f"Found {count} occurrences of {label} in {path}. {subject} must be unique. Please provide more context to make it unique.")
        matched.append((index, _Replacement(start, start + len(needle), new)))

    matched.sort(key=lambda pair: pair[1].start)
    for (previous_index, previous), (index, current) in zip(matched, matched[1:], strict=False):
        if previous.end > current.start:
            raise ValueError(f"edits[{previous_index}] and edits[{index}] overlap in {path}. Merge them into one edit or target disjoint regions.")
    changes = [change for _, change in matched]
    return _widen_fuzzy(content, fuzzy, changes) if use_fuzzy else changes


def _preserve_endings(original: str, changes: list[_Replacement]) -> str:
    crlf = [match.start() - index for index, match in enumerate(re.finditer("\r\n", original))]
    parts: list[str] = []

    def append(text: str) -> None:
        if text:
            if parts and parts[-1].endswith("\r") and text.startswith("\n"):
                parts.append("\n")
            parts.append(text)

    position = 0
    for change in changes:
        start = change.start + bisect_right(crlf, change.start - 1)
        end = change.end + bisect_right(crlf, change.end - 1)
        newline = original.find("\n", start)
        if newline < 0:
            newline = original.rfind("\n", 0, start + 1)
        if newline < 0:
            ending = "\r" if "\r" in original else "\n"
        else:
            ending = "\r\n" if newline > 0 and original[newline - 1] == "\r" else "\n"
        append(original[position:start])
        append(change.text.replace("\n", ending))
        position = end
    append(original[position:])
    return "".join(parts)


def pi_edit(path: str, edits: list[dict[str, str]], *, context: ToolContext) -> str:
    if not edits:
        return "ERROR: Edit tool input is invalid. edits must contain at least one replacement."
    try:
        target = _path(path, context, write=True)
        with _MUTATION_LOCKS[hash(target) % len(_MUTATION_LOCKS)]:
            try:
                if not os.access(target, os.R_OK | os.W_OK):
                    code = "EACCES" if target.exists() else "ENOENT"
                    return f"ERROR: Could not edit file: {path}. Error code: {code}."
                original = file_bytes.read_regular_bytes(target).decode("utf-8", errors="replace")
            except OSError as exc:
                code = errno.errorcode.get(exc.errno)
                if code:
                    return f"ERROR: Could not edit file: {path}. Error code: {code}."
                raise
            bom = "\ufeff" if original.startswith("\ufeff") else ""
            original = original.removeprefix(bom)
            normalized = file_bytes.normalize_line_endings(original, "\n")
            changes = _match_edits(normalized, edits, path)
            updated = _splice(normalized, changes)
            if updated == normalized:
                detail = ("The replacement produced identical content. This might indicate an issue with "
                          "special characters or the text not existing as expected.") if len(edits) == 1 else (
                              "The replacements produced identical content.")
                return f"ERROR: No changes made to {path}. {detail}"
            final = updated if original == normalized else _preserve_endings(original, changes)
            context.invalidate_search_cache()
            target.write_bytes((bom + final).encode())
    except (OSError, ValueError) as exc:
        return f"ERROR: {exc}"
    return f"Successfully replaced {len(edits)} block(s) in {path}."


def pi_write(path: str, content: str, *, context: ToolContext) -> str:
    try:
        target = _path(path, context, write=True)
        data = content.encode()
        with _MUTATION_LOCKS[hash(target) % len(_MUTATION_LOCKS)]:
            if target.exists() and not target.is_file():
                raise OSError(f"not a regular file: {target}")
            context.invalidate_search_cache()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    except (OSError, ValueError) as exc:
        return f"ERROR: {exc}"
    return f"Successfully wrote to {path}"


def _single_edit(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(value.get(key), str) for key in ("oldText", "newText"))


def _prepare_edits(arguments: dict) -> dict:
    args = dict(arguments)
    edits = args.get("edits")
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except ValueError:
            pass
    if _single_edit(edits):
        edits = [edits]
    if _single_edit(args):
        edits = [*(edits if isinstance(edits, list) else []),
                 {"oldText": args.pop("oldText"), "newText": args.pop("newText")}]
    if edits is not None:
        args["edits"] = edits
    return args


def tools() -> tuple[Tool, ...]:
    def parameter(tool: str, field: str, kind: str = "string") -> dict:
        return {"type": kind, "description": load_parameter_description(tool, field)}

    return (
        Tool("pi_read", load_description("pi_read"), pi_read, {
            "path": parameter("pi_read", "path"),
            "offset": parameter("pi_read", "offset", "number"),
            "limit": parameter("pi_read", "limit", "number"),
        }, required=("path",), read_only=True, presentation=Presentation(key=("path",), fold_noun="read")),
        Tool("pi_edit", load_description("pi_edit"), pi_edit, {
            "path": parameter("pi_edit", "path"),
            "edits": {
                **parameter("pi_edit", "edits", "array"),
                "items": {
                    "type": "object",
                    "properties": {
                        key: parameter("pi_edit", f"edits.items.{key}") for key in ("oldText", "newText")
                    },
                    "required": ["oldText", "newText"],
                },
            },
        }, required=("path", "edits"), prepare_args=_prepare_edits,
            presentation=Presentation(key=("path",), unlisted=("edits",), note="edits")),
        Tool("pi_write", load_description("pi_write"), pi_write, {
            "path": parameter("pi_write", "path"),
            "content": parameter("pi_write", "content"),
        }, required=("path", "content"), presentation=Presentation(key=("path",), unlisted=("content",))),
    )
