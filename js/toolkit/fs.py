"""Filesystem tools with read-before-write safety rails."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

from .. import held, imagefit, jail, settings as _settings, tool_args as _tool_args
from ..media import IMAGE_RESULT_PREFIX
from . import policy
from .core import Output, ToolContext, view as _view
from .path_hints import no_such_file
from .reads import require_read
from .results import truncate
from .sanitize import int_or_default
from .wiki.helpers import run

_TEXT_EXTS = {
    ".txt", ".md", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".toml",
    ".yaml", ".yml", ".json", ".sh", ".bash", ".zsh", ".css", ".html", ".xml",
    ".sql", ".go", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php",
}
_BINARY_EXTS = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".obj", ".o", ".class", ".pyc",
    ".jar", ".war", ".ear", ".zip", ".tar", ".gz", ".rar", ".7z", ".iso",
    ".img", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".bmp",
    ".ico", ".mp3", ".mp4", ".avi", ".mov", ".sqlite", ".db", ".png", ".jpg",
    ".jpeg", ".gif", ".webp",
}
_IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _resolve_path_no_follow(context: ToolContext, raw: str | os.PathLike[str], *,
                            write: bool = False) -> Path:
    return context.resolve_path(raw, write=write, follow=False)


def _path_size_no_follow(path: Path, *, cap: int | None = None) -> int:
    if cap is None:
        cap = int(_settings.default_value("TRASH_BYTES_MAX")) + 1
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        total = path.lstat().st_size
        if path.is_dir():
            for root, dirs, files in os.walk(path, followlinks=False):
                for name in (*dirs, *files):
                    try:
                        total += (Path(root) / name).lstat().st_size
                    except OSError:
                        continue
                    if total > cap:
                        return total
        return total
    except OSError:
        return cap


def _trash_command() -> str | None:
    for command in ("trash", "trash-put"):
        found = shutil.which(command)
        if found:
            return found
    return None


def _snapshot_remove_target(context: ToolContext, target: Path) -> None:
    if target.is_symlink():
        context.undo.record(target, {"kind": "symlink", "target": os.readlink(target)})
        return
    context.snapshot(target)


def _delete_target_no_follow(target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def _trash_target(target: Path, context: ToolContext) -> str | None:
    command = _trash_command()
    if not command:
        return "ERROR: trash command not found; pass permanent=true to delete without trash."
    rc, _out, err = run([command, str(target)], context=context, timeout=120)
    if rc != 0:
        return f"ERROR: trash failed: {err.strip() or f'exit {rc}'}; pass permanent=true to delete without trash."
    return None


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _result_page_bytes(context: ToolContext) -> int:
    """Bytes of text one tool result can carry to the model whole: half the
    tighter of the inline spill cap and the hard result cap, leaving room for
    the text around it. 0 when neither cap is set."""
    caps = [
        cap
        for cap in (
            int(getattr(context, "max_tool_result_inline_bytes", 0) or 0),
            int(getattr(context, "max_tool_result_bytes", 0) or 0),
        )
        if cap > 0
    ]
    return min(caps) // 2 if caps else 0


def _render_line_spans(spans: list[tuple[int, int]]) -> str:
    return ", ".join(str(start) if start == end else f"{start}-{end}" for start, end in spans)


def changed_since_read(
    context: ToolContext,
    target: Path,
    action: str,
    current: bytes,
    current_hash: str,
    *,
    edits_lines: bool,
) -> str | None:
    """Refuse an edit of a file that changed on disk since the model read it,
    and hand the model the diff from the content it read to the content there now.

    Read coverage moves to the current content: lines the model had seen that
    the change left alone keep counting as seen, and every current line the
    diff shows (changed lines and their context) counts as seen when the diff
    is delivered whole, so the retry needs no second read.
    A diff over the result budget is replaced by the changed line numbers.
    *edits_lines* is True for patch, which stays gated on those lines until
    they are read; an overwrite discards them, so its retry goes through.
    Returns None when the file is unchanged since the read, or when js holds no
    UTF-8 copy of the content that was read; require_read covers those cases.
    """
    as_read = context.reads.content_as_read(target)
    if as_read is None or as_read[0] == current_hash:
        return None
    known_hash, known = as_read
    try:
        old_text = known.decode("utf-8")
        new_text = current.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # The same line split and matcher unified_diff uses, so the hunks
    # credited below are the hunks the model is shown.
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    coverage = context.reads.coverage(target)
    was_whole = coverage.whole
    prior = [(1, len(old_lines))] if was_whole else coverage.ranges
    carried: list[tuple[int, int]] = []
    changed: list[tuple[int, int]] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for seen_start, seen_end in prior:
                low, high = max(seen_start, i1 + 1), min(seen_end, i2)
                if low <= high:
                    carried.append((low - i1 + j1, high - i1 + j1))
        elif j2 > j1:
            changed.append((j1 + 1, j2))
    shown = [
        (group[0][3] + 1, group[-1][4])
        for group in matcher.get_grouped_opcodes(3)
        if group[-1][4] > group[0][3]
    ]
    diff = unified_diff(old_text, new_text, f"{target} (as read)", f"{target} (now)")
    budget = _result_page_bytes(context)
    delivered = not budget or len(diff.encode("utf-8")) <= budget
    context.reads.replace(
        target,
        current_hash,
        carried + (shown if delivered else []),
        len(new_lines),
        whole_file=was_whole and delivered,
    )
    context.reads.remember_content(target, current_hash, current)
    head = (
        f'ERROR: {target} changed on disk since it was read (hash {known_hash} when read, {current_hash} now); nothing was written.'
    )
    if delivered:
        return f'{head} Diff from what you read to what is there now:\n{diff}Retry to {action} against the current text.'
    spans = _render_line_spans(changed) or 'none (lines were only removed)'
    if not edits_lines:
        return (
            f'{head} The diff is too large to show; the changed lines are now {spans}. A retry will {action} and discard them (undo restores them); read them first if you need what changed.'
        )
    return (
        f'{head} The diff is too large to show; the changed lines are now {spans}. Read those, then retry to {action}.'
    )


def _read_regular_bytes(path: Path, limit: int | None = None) -> bytes:
    """Read bytes from *path*, refusing anything that is not a regular file and
    never blocking on a FIFO/socket/device.

    O_NONBLOCK makes the open return immediately for a pipe with no writer (the
    kernel would otherwise park the caller in fifo_open->wait_for_partner);
    on a regular file the flag has no effect and the read proceeds normally.
    Raises OSError for a non-regular file or any read error so callers degrade
    exactly as they do for a plain OSError."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
        chunks: list[bytes] = []
        read_so_far = 0
        while limit is None or read_so_far < limit:
            want = 65536 if limit is None else min(65536, limit - read_so_far)
            chunk = os.read(fd, want)
            if not chunk:
                break
            chunks.append(chunk)
            read_so_far += len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _is_binary(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_EXTS:
        return True
    if path.suffix.lower() in _TEXT_EXTS:
        return False
    try:
        chunk = _read_regular_bytes(path, 4096)
    except OSError:
        return False
    return b"\x00" in chunk


def _detect_visual_mime(path: Path, header: bytes) -> str | None:
    suffix_mime = _IMAGE_MIME_BY_EXT.get(path.suffix.lower())
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if suffix_mime:
        return suffix_mime
    if header.startswith(b"%PDF-") or path.suffix.lower() == ".pdf":
        return "application/pdf"
    return None


def _visual_fallback(path: Path, mime: str, size: int) -> str:
    return f"VISUAL_FILE {path} mime={mime} size={size} bytes (vision disabled; no image bytes returned)"


def _image_marker(path: Path, mime: str, size: int, data: bytes) -> str:
    """The marker the model client sends ``path``'s image ``data`` by. An
    image over imagefit's limits is sent as its fitted copy, and the text
    says what changed."""
    stub = f"VISUAL_FILE {path} mime={mime} size={size} bytes"
    fitted = imagefit.fit(data, mime)
    if fitted is None or not fitted.changed:
        return f"{IMAGE_RESULT_PREFIX}{path}\t{mime}\t{stub}"
    return f"{IMAGE_RESULT_PREFIX}{imagefit.stored(fitted)}\t{fitted.mime}\t{stub} {fitted.note}"


def _read_pdf_text(path: Path, context: ToolContext) -> tuple[str, bytes]:
    data = _read_regular_bytes(path)
    rc, out, err = run(["pdftotext", str(path), "-"], context)
    if rc == 0 and out.strip():
        return truncate(out, context), data
    detail = err.strip() or "no extractable text"
    return f"ERROR: pdftotext failed for {path}: {detail}", data


def _read_text(path: Path, context: ToolContext) -> tuple[str, bytes]:
    data = _read_regular_bytes(path)
    if len(data) > context.max_file_bytes:
        raise ValueError(
            f"File size ({len(data)} bytes) exceeds the maximum allowed size of {context.max_file_bytes} bytes"
        )
    if _is_binary(path):
        raise ValueError("Binary or visual files are not supported by this Python port yet")
    return data.decode("utf-8"), data


def format_numbered_lines(lines: list[str], start_line: int) -> str:
    out: list[str] = []
    for idx, line in enumerate(lines, start=start_line):
        out.append(f"{idx}|{line}")
    return "\n".join(out)


def detect_line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    return "\r" if "\r" in text and "\n" not in text else "\n"


def normalize_line_endings(text: str, target: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", target)


def _encode_preserving_existing_newlines(path: Path, content: str) -> bytes:
    line_ending = "\n"
    if path.exists():
        try:
            existing = path.read_bytes().decode("utf-8")
            line_ending = detect_line_ending(existing)
        except UnicodeDecodeError:
            line_ending = "\n"
    normalized = normalize_line_endings(content, line_ending)
    return normalized.encode("utf-8")


def _utf8_char_start(buf: bytes, index: int) -> int:
    """Move *index* back to the first byte of the UTF-8 character it lands in."""
    back = 0
    while 0 < index < len(buf) and back < 3 and (buf[index] & 0xC0) == 0x80:
        index -= 1
        back += 1
    return index


def _read_byte_call(target: Path, start_byte: int) -> str:
    """JSON arguments for a follow-up byte-range `read` call."""
    return json.dumps({"file_path": str(target), "start_byte": start_byte})


def _read_byte_range(target: Path, context: ToolContext, start_byte: int, end_byte: int | None) -> str:
    """Return the text between two byte offsets of *target*, one page at most.

    A page is what one tool result carries whole (_result_page_bytes), so a
    page of a spilled result is never spilled again. Both ends move back to a
    UTF-8 character start and the footer names the offsets actually returned.
    The lines the page shows whole count as read, for files of at most
    FILE_BYTES_MAX; a larger file is read by seeking and records no
    coverage."""
    try:
        size = target.stat().st_size
    except OSError as exc:
        return f"ERROR: {exc}"
    if size == 0:
        return f"{target} is empty"
    if end_byte is not None and end_byte < start_byte:
        start_byte, end_byte = end_byte, start_byte
    if start_byte >= size:
        return f"{target} has {size} bytes; requested start_byte={start_byte} is past EOF"
    page = _result_page_bytes(context) or size
    stop = min(size, start_byte + page, end_byte if end_byte is not None else size)
    if stop <= start_byte:
        return f"ERROR: empty byte range {start_byte}-{end_byte}"
    whole: bytes | None = None
    try:
        if size <= context.max_file_bytes:
            whole = _read_regular_bytes(target)
            size = len(whole)
            base, buf = 0, whole
        else:
            base = max(0, start_byte - 3)
            with target.open("rb") as handle:
                handle.seek(base)
                buf = handle.read(min(size, stop + 1) - base)
    except OSError as exc:
        return f"ERROR: {exc}"
    start = base + _utf8_char_start(buf, start_byte - base)
    end = size if stop >= size else base + _utf8_char_start(buf, stop - base)
    if end <= start:
        end = min(size, stop)
    text = buf[start - base:end - base].decode("utf-8", errors="replace")
    if whole is not None:
        _remember_byte_read(target, context, whole, start, end)
    _view(path=str(target), start_byte=start, end_byte=end, total_bytes=size, text=text)
    if end < size:
        footer = f"[bytes {start}-{end} of {size}; continue with {_read_byte_call(target, end)}]"
    else:
        footer = f"[bytes {start}-{end} of {size}; end of file]"
    return Output(f"{text}\n{footer}")


def _remember_byte_read(target: Path, context: ToolContext, data: bytes, start: int, end: int) -> None:
    """Record the lines that bytes start..end of *data* show whole, numbered
    the way the line reader numbers them."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    content_hash = hash_bytes(data)
    lines = text.splitlines(keepends=True)
    shown: list[tuple[int, int]] = []
    offset = 0
    for number, line in enumerate(lines, start=1):
        line_end = offset + len(line.encode("utf-8"))
        if offset >= start and line_end <= end:
            if shown and shown[-1][1] == number - 1:
                shown[-1] = (shown[-1][0], number)
            else:
                shown.append((number, number))
        offset = line_end
        if offset >= end:
            break
    if shown:
        first, last = shown[0]
        context.reads.remember(
            target,
            content_hash,
            start_line=first,
            end_line=last,
            total_lines=len(lines),
            whole_file=first == 1 and last == len(lines),
        )
    else:
        context.reads.remember(target, content_hash, total_lines=len(lines), whole_file=False)
    context.reads.remember_content(target, content_hash, data)


_READ_RANGE_FIELDS = ("start_line", "end_line", "start_byte", "end_byte")
_READ_RANGE_ALIASES = {"line_start": "start_line", "line_end": "end_line"}
_READ_RANGE_WORDS = re.compile(r"\b(start_line|end_line|start_byte|end_byte|line_start|line_end)\b\W*?(\d+)")
_READ_LINE_SPAN = re.compile(r"(\d+)(?:\s*(?:-|,|:|\.\.)\s*(\d+)?)?")


def _plain_int(value: object) -> int | None:
    """``value`` as an int when it is one or a string of digits, else None."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _range_fields(raw: object) -> dict | None:
    """The fields a `range` value names, or None when it names none.

    Takes the object, the object as JSON text (a missing closing brace is
    mended), `start_line 170`, a line span written `170-185`, `1,110`,
    `170:185` or `170`, and a list `[170, 185]`."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list) and 1 <= len(raw) <= 2 and all(_plain_int(item) is not None for item in raw):
        return dict(zip(("start_line", "end_line"), (_plain_int(item) for item in raw)))
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("{"):
        try:
            return _tool_args.repair_jsonish(text)
        except ValueError:
            pass
    named = _READ_RANGE_WORDS.findall(text)
    if named:
        return {key: int(value) for key, value in named}
    span = _READ_LINE_SPAN.fullmatch(text)
    if span is None:
        return None
    fields = {"start_line": int(span[1])}
    if span[2]:
        fields["end_line"] = int(span[2])
    return fields


def prepare_read_arguments(arguments: dict) -> dict:
    """`read`'s arguments with the range shapes models send mapped onto the
    schema's flat `start_line`/`end_line`/`start_byte`/`end_byte`.

    A `range` value (see _range_fields) is flattened, its fields filling only
    the ones the call does not name at the top level. `line_start`/`line_end`
    are `start_line`/`end_line`; `offset` is the first line and `limit` the
    number of lines. A `range` that names nothing, or keys it holds that are
    not range fields, stay under `range` for validation to report."""
    args = {key: value for key, value in arguments.items() if not (key == "range" and value is None)}
    fields: dict[str, object] = {}

    def take(source: dict) -> None:
        for key in list(source):
            name = _READ_RANGE_ALIASES.get(key, key)
            if name in _READ_RANGE_FIELDS or name in ("offset", "limit"):
                value = source.pop(key)
                if value is not None:
                    fields.setdefault(name, value)

    take(args)
    if "range" in args:
        nested = _range_fields(args["range"])
        if nested is not None:
            take(nested)
            if nested:
                args["range"] = nested
            else:
                del args["range"]
    offset, limit = fields.pop("offset", None), fields.pop("limit", None)
    first, count = _plain_int(offset), _plain_int(limit)
    if (offset is not None and first is None) or (limit is not None and count is None):
        # Not a line count: left as sent, so validation names it.
        if offset is not None:
            args["offset"] = offset
        if limit is not None:
            args["limit"] = limit
    elif first is not None or count is not None:
        start = max(1, first or 1)
        fields.setdefault("start_line", start)
        if count:
            fields.setdefault("end_line", start + count - 1)
    args.update(fields)
    return args


def fs_read(
    path: str | None = None,
    file_path: str | None = None,
    range: dict | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    show_line_numbers: bool = True,
    context: ToolContext | None = None,
    start_byte: int | None = None,
    end_byte: int | None = None,
) -> str:
    assert context is not None
    raw_path = file_path or path
    if not raw_path:
        return "ERROR: file_path is required"
    asked = prepare_read_arguments({
        "start_line": start_line, "end_line": end_line,
        "start_byte": start_byte, "end_byte": end_byte, "range": range,
    })
    if set(asked) - set(_READ_RANGE_FIELDS):
        return f'ERROR: range {range!r} names no lines or bytes; pass start_line/end_line or start_byte/end_byte.'
    start_line, end_line = asked.get("start_line"), asked.get("end_line")
    start_byte, end_byte = asked.get("start_byte"), asked.get("end_byte")
    for name, value in (("start_byte", start_byte), ("end_byte", end_byte)):
        if value is not None and int_or_default(value, -1, minimum=0) == -1:
            return f"ERROR: {name} must be a non-negative integer, got {value!r}"
    byte_start = int_or_default(start_byte, -1, minimum=0)
    byte_end = int_or_default(end_byte, -1, minimum=0)
    byte_ranged = byte_start != -1 or byte_end != -1
    # A whole-file read (no range asked for) is the only one gated by
    # max_read_bytes and max_file_bytes. Once the caller names a range it is
    # reading deliberately: a line range of a file over max_file_bytes is
    # streamed, and a byte range seeks.
    # Matches the acceptance rule the line math below uses: bools, junk and
    # out-of-range values are not a range, so they can't smuggle a whole-file
    # read past the cap.
    ranged = (
        int_or_default(start_line, -1, minimum=1) != -1
        or int_or_default(end_line, -1, minimum=1) != -1
    )
    # A line range and a byte range together: the line range is read, and the
    # result says the byte range was not.
    note = ""
    if byte_ranged and ranged:
        byte_ranged = False
        note = "\n" + '[start_byte/end_byte ignored: a line range was given too, and the line range was read]'
    target = context.resolve_path(raw_path)
    if not target.exists():
        return no_such_file(context, target)
    if not target.is_file():
        return f"ERROR: not a regular file: {target}"
    if target.suffix.lower() == ".ipynb" and not ranged and not byte_ranged:
        from . import notebook

        view = notebook.read_view(target, context)
        if view is not None:
            return view

    try:
        size = target.stat().st_size
        header = _read_regular_bytes(target, 16)
    except OSError as exc:
        return f"ERROR: {exc}"

    mime = _detect_visual_mime(target, header)
    if byte_ranged:
        if mime or _is_binary(target):
            return f"ERROR: byte ranges read text files; {target} is not text"
        return _read_byte_range(target, context, max(0, byte_start), None if byte_end == -1 else byte_end)
    if mime and mime.startswith("image/"):
        try:
            data = _read_regular_bytes(target)
        except OSError as exc:
            return f"ERROR: {exc}"
        content_hash = hash_bytes(data)
        context.reads.remember(target, content_hash)
        if not context.vision_enabled:
            return _visual_fallback(target, mime, size)
        return _image_marker(target, mime, size, data)

    if mime == "application/pdf":
        # A PDF is read whole — pdftotext extracts the entire document and has no
        # ranged mode — so the file cap applies before the bytes are loaded.
        if size > context.max_file_bytes:
            return f"ERROR: file size ({size} bytes) exceeds the maximum allowed size of {context.max_file_bytes} bytes"
        try:
            text, data = _read_pdf_text(target, context)
        except OSError as exc:
            return f"ERROR: {exc}"
        content_hash = hash_bytes(data)
        context.reads.remember(target, content_hash)
        return text

    read_cap = int(context.max_read_bytes or 0)
    if not ranged and read_cap > 0 and size > read_cap:
        example = _read_call(target, 1, context.max_read_lines)
        return (
            f'ERROR: file size ({size} bytes) exceeds READ_BYTES_MAX ({read_cap}) for a whole-file read. Read a range instead, e.g. {example} — ranged reads are not subject to this cap.'
        )
    if not ranged and size > context.max_file_bytes:
        example = _read_call(target, 1, context.max_read_lines)
        return f'ERROR: file size ({size} bytes) exceeds FILE_BYTES_MAX ({context.max_file_bytes}) for a whole-file read. Read it by line range instead, e.g. {example}'

    # Resolve the window: a reversed range is
    # swapped, an oversized one is clamped to max_read_lines, and an omitted
    # end_line means "one page starting at start_line" — NOT "one page starting at
    # line 1", which used to make the tool's own
    # `read ... with start_line=N to continue` hint fail with an invalid-range error.
    raw_start = int_or_default(start_line, 1, minimum=1)
    raw_end = int_or_default(end_line, 0, minimum=1)
    if raw_end and raw_end < raw_start:
        raw_start, raw_end = raw_end, raw_start
    start = max(1, raw_start)
    page_end = start + context.max_read_lines - 1
    last = min(page_end, raw_end or page_end)
    numbered = bool(show_line_numbers)

    if size > context.max_file_bytes:
        if _is_binary(target):
            return "ERROR: Binary or visual files are not supported by this Python port yet"
        return _read_streamed_lines(target, size, start, last, numbered, note)

    try:
        text, data = _read_text(target, context)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return f"ERROR: {exc}"

    content_hash = hash_bytes(data)

    # File lines end at LF, CRLF or CR; Unicode separators remain content.
    all_lines = re.split(r"\r\n|\r|\n", text)
    if not all_lines[-1]:
        all_lines.pop()
    total = len(all_lines)
    if total == 0:
        context.reads.remember(target, content_hash, total_lines=0, whole_file=True)
        context.reads.remember_content(target, content_hash, data)
        _view(path=str(target), start_line=0, end_line=0, total_lines=0, lines=[])
        return f"{target} is empty (hash {content_hash}){note}"

    if start > total:
        return f"{target} has {total} total lines; requested start_line={start} is past EOF (hash {content_hash}){note}"
    end = min(total, last)

    selected = all_lines[start - 1:end]
    read_key = (content_hash, start, end, numbered)
    earlier = context.reads.shown_by(target, read_key)
    context.reads.remember(
        target,
        content_hash,
        start_line=start,
        end_line=end,
        total_lines=total,
        whole_file=start == 1 and end == total,
    )
    context.reads.remember_content(target, content_hash, data)
    _view(path=str(target), start_line=start, end_line=end, total_lines=total, lines=selected)
    suffix = ""
    if end < total:
        suffix = f"\n[{total} total lines; continue with {_read_call(target, end + 1)}]"
    suffix += note
    if context.read_deduplicate and earlier is not None:
        return (
            f"Lines {start}-{end} are unchanged since read call {earlier} returned them "
            f"(hash {content_hash}); that result is current.{suffix}"
        )
    # Lines are returned whole: `read` pages by line, not by column, so a cut
    # line is unreachable content. max_read_bytes/max_read_lines bound the read,
    # and the tool-result spill bounds what reaches the model.
    body = format_numbered_lines(selected, start) if numbered else "\n".join(selected)
    result = Output(f"{body}{suffix}")
    context.reads.offer(target, read_key, result)
    return result


def _read_streamed_lines(target: Path, size: int, start: int, last: int, numbered: bool, note: str) -> str:
    """Lines start..last of a text file over FILE_BYTES_MAX, read one line at
    a time and never held whole. Lines end at LF, CRLF or CR, as in the
    whole-file reader. The read records no coverage: the file is never hashed."""
    selected: list[str] = []
    more = False
    count = 0
    try:
        with open(target, encoding="utf-8", newline="") as handle:
            for count, line in enumerate(handle, start=1):
                if count > last:
                    more = True
                    break
                if count >= start:
                    selected.append(line.rstrip("\r\n"))
    except (OSError, UnicodeDecodeError) as exc:
        return f"ERROR: {exc}"
    if not selected:
        return f"{target} has {count} total lines; requested start_line={start} is past EOF{note}"
    end = start + len(selected) - 1
    _view(path=str(target), start_line=start, end_line=end, total_lines=None, lines=selected)
    footer = ""
    if more:
        footer = "\n" + f'[file is {size} bytes, too large to count its lines; continue with {_read_call(target, end + 1)}]'
    body = format_numbered_lines(selected, start) if numbered else "\n".join(selected)
    return Output(f"{body}{footer}{note}")


def _read_call(target: Path, start_line: int, end_line: int | None = None) -> str:
    """JSON arguments for a follow-up `read` call, in the public schema's shape."""
    args: dict[str, str | int] = {"file_path": str(target), "start_line": start_line}
    if end_line is not None:
        args["end_line"] = end_line
    return json.dumps(args)


read = fs_read


def write(file_path: str | None = None, content: str | None = None, overwrite: bool = False, context: ToolContext | None = None, path: str | None = None, expected_hash: str | None = None, from_held: str | None = None, append: bool = False) -> str:
    assert context is not None
    raw_path = file_path or path
    source = None
    if from_held:
        try:
            source = held.load(context, from_held)
        except held.HeldError as exc:
            return f"ERROR: {exc}"
        if content and not append:
            return 'ERROR: content and from_held together need append=true, which adds content after the held text'
        text = source.text + (content or "")
    elif content is None:
        return 'ERROR: content or from_held is required'
    else:
        text = content
    try:
        result = _write_text(context, raw_path, text, overwrite=overwrite, expected_hash=expected_hash,
                             append=append and source is None)
    except jail.JailError as exc:
        result = jail.Refusal(f"ERROR: {exc}")
    if not result.startswith("ERROR"):
        if source is not None:
            held.release(source)
        return result
    # The content stays held, so the retry names it instead of sending it again.
    if source is not None and text == source.text:
        note = held.held_note(context, source, raw_path)
    else:
        note = held.hold_failed_write(context, text, raw_path, append=append and source is None)
    return type(result)(result + note) if note else result


def _write_text(context: ToolContext, raw_path: str | None, content: str, *, overwrite: bool,
                expected_hash: str | None, append: bool) -> str:
    """Write ``content`` to ``raw_path``, or add it to the end with ``append``.
    Raises JailError for a path the jail refuses."""
    if not raw_path:
        return "ERROR: file_path is required"
    target = context.resolve_path(raw_path, write=True)
    if target.exists() and context.write_require_overwrite and not overwrite and not append:
        return (
            f'ERROR: {raw_path} already exists. To change part of it use patch; to replace it whole, read it first and pass overwrite=true.'
        )
    replacing = target.exists()
    if replacing:
        # One read of any page is the gate: it shows the model what the file
        # is. The hash proves nothing moved since; the snapshot makes the
        # discard undoable.
        try:
            current = _read_regular_bytes(target)
        except OSError as exc:
            return f"ERROR: {exc}"
        current_hash = hash_bytes(current)
        action = "append to it" if append else "overwrite it"
        if expected_hash:
            # The hash the model expects is its proof of what it replaces: a
            # match needs no read, a mismatch names both and writes nothing.
            guard = None if expected_hash.strip() == current_hash else (
                f'ERROR: {target} has hash {current_hash}, not the expected {expected_hash.strip()}; nothing was written. Read it, then retry.'
            )
        else:
            guard = changed_since_read(
                context, target, action, current, current_hash, edits_lines=False
            ) or require_read(context, target, action, content_hash=current_hash)
        if guard:
            return guard
    try:
        data = _encode_preserving_existing_newlines(target, content)
    except (OSError, UnicodeEncodeError) as exc:
        return f"ERROR: {exc}"
    appending = append and replacing
    written = current + data if appending else data
    try:
        context.snapshot(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(written)
    except OSError as exc:
        return f"ERROR: {exc}"
    _view(path=str(target), before=current.decode("utf-8", errors="replace") if replacing else None,
         after=written.decode("utf-8", errors="replace"), bytes=len(written))
    content_hash = hash_bytes(written)
    total = len(written.decode("utf-8", errors="replace").splitlines())
    if appending:
        # The model has seen what it read of the old content and every line
        # it added; a line the old content left unfinished continues there.
        coverage = context.reads.coverage(target)
        same = coverage.content_hash == current_hash
        whole = same and coverage.whole
        ranges = coverage.ranges if same else []
        old_total = len(current.decode("utf-8", errors="replace").splitlines())
        first = old_total if current and not current.endswith(b"\n") else old_total + 1
        if data and first <= total:
            ranges.append((max(1, first), total))
        context.reads.replace(target, content_hash, [] if whole else ranges, total, whole_file=whole)
        context.reads.remember_content(target, content_hash, written)
        return f"appended {len(data)} bytes to {target} (hash {content_hash})"
    # The model wrote every byte of the new content, so the file counts as
    # fully read: a patch can follow without a read in between.
    context.reads.replace(target, content_hash, [], total, whole_file=True)
    context.reads.remember_content(target, content_hash, written)
    return f"wrote {len(data)} bytes to {target} (hash {content_hash})"


def remove(path: str, permanent: bool | None = False, context: ToolContext | None = None) -> str:
    assert context is not None
    target = _resolve_path_no_follow(context, path, write=True)
    if not target.exists() and not target.is_symlink():
        return f"ERROR: no such path: {target}"
    try:
        maximum = int(_settings.knob_attr(context, "trash_bytes_max", "TRASH_BYTES_MAX"))
        size = _path_size_no_follow(target, cap=maximum + 1)
        permanent, refusal = policy.removal_decision(context, permanent=permanent, size=size)
        if refusal is not None:
            return refusal
        _snapshot_remove_target(context, target)
        if not permanent:
            error = _trash_target(target, context)
            if error:
                context.undo.discard(target)
                return error
            _view(path=str(target), trashed=True)
            return f"trashed {target}"
        _delete_target_no_follow(target)
    except OSError as exc:
        return f"ERROR: {exc}"
    _view(path=str(target), trashed=False)
    return f"removed {target}"


def undo(path: str, context: ToolContext | None = None) -> str:
    assert context is not None
    # A restore rewrites bytes outside the write/patch path, so drop memoized
    # searches rather than let a directory-root result outlive the restore.
    context.invalidate_search_cache()
    # write/patch key snapshots under resolve_path (follows symlinks); remove keys
    # under the no-follow abspath. Try both so undo finds the snapshot either laid it.
    target = context.resolve_path(path, write=True)
    if not context.undo.count(target):
        no_follow = _resolve_path_no_follow(context, path, write=True)
        if context.undo.count(no_follow):
            target = no_follow
    try:
        with context.undo.restoring(target) as (found, previous):
            if not found:
                return f"ERROR: no snapshot available for {target}"
            if isinstance(previous, dict) and previous.get("kind") in {"corrupt", "unavailable"}:
                reason = str(previous.get("reason", "snapshot data is unavailable"))
                return f"ERROR: discarded unusable snapshot for {target}: {reason}; retry undo for an older entry"
            if isinstance(previous, dict) and previous.get("kind") == "symlink":
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(previous["target"])
                return f"restored symlink {target}"
            if isinstance(previous, dict) and previous.get("kind") == "directory":
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
                target.mkdir(parents=True, exist_ok=True)
                modes = previous.get("modes", {})
                entries = previous.get("entries", {})
                for rel, data in entries.items():
                    child = target / rel.rstrip("/")
                    if isinstance(data, dict):
                        child.parent.mkdir(parents=True, exist_ok=True)
                        child.symlink_to(data["target"])
                    elif rel.endswith("/"):
                        child.mkdir(parents=True, exist_ok=True)
                    else:
                        child.parent.mkdir(parents=True, exist_ok=True)
                        if rel in modes:
                            child.touch(mode=0o600)
                        child.write_bytes(data or b"")
                        if rel in modes:
                            child.chmod(modes[rel])
                for rel in sorted(modes, key=lambda rel: len(Path(rel).parts), reverse=True):
                    if rel == "." or rel.endswith("/"):
                        (target / rel).chmod(modes[rel])
                return f"restored directory {target}"
            if previous is None:
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
                return f"restored deletion state for {target}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                target.unlink()
            mode = None
            coverage = None
            if isinstance(previous, dict):
                mode = previous["mode"]
                coverage = previous.get("read_coverage")
                previous = previous["data"]
                if not target.exists():
                    target.touch(mode=0o600)
            target.write_bytes(previous)
            if mode is not None:
                target.chmod(mode)
            content_hash = hash_bytes(previous)
            # Legacy snapshots have no evidence of what was displayed. Never
            # carry transformed coverage from the edited version across undo.
            valid = isinstance(coverage, dict) and coverage.get("hash") == content_hash
            context.reads.replace(
                target, content_hash,
                [tuple(span) for span in coverage["ranges"]] if valid else [],
                len(previous.decode("utf-8", errors="replace").splitlines()),
                whole_file=bool(valid and coverage["whole"]),
            )
            context.reads.remember_content(target, content_hash, previous)
            return f"restored {target} (hash {content_hash})"
    except OSError as exc:
        return f"ERROR: {exc}"


def unified_diff(source: str, updated: str, fromfile: str, tofile: str) -> str:
    """Unified diff with the standard no-final-newline marker.

    difflib never emits ``\\ No newline at end of file``, so the last
    removed/added line of a file with no trailing newline is concatenated onto
    the next diff line, producing invalid unified-diff syntax."""
    chunks: list[str] = []
    for line in difflib.unified_diff(
        source.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
    ):
        chunks.append(line)
        if line and not line.endswith("\n"):
            chunks.append("\n\\ No newline at end of file\n")
    return "".join(chunks)
