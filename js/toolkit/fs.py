"""Filesystem tools with read-before-write safety rails."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import threading
from functools import lru_cache
from pathlib import Path
from collections.abc import Iterable

from ..tool_binaries import resolve_binary
from .core import Tool, ToolContext
from .sanitize import int_or_default
from .wiki.helpers import run
from .descriptions import load_description


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
_IMAGE_RESULT_PREFIX = "IMAGE_RESULT\t"
_TRASH_MAX_BYTES = 512 * 1024 * 1024


def _resolve_path_no_follow(context: ToolContext, raw: str | os.PathLike[str]) -> Path:
    path = Path(os.path.expanduser(str(raw)))
    if not path.is_absolute():
        path = context.cwd / path
    return Path(os.path.abspath(path))


def _path_size_no_follow(path: Path, *, cap: int = _TRASH_MAX_BYTES + 1) -> int:
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
        context.snapshots.setdefault(target, []).append({"kind": "symlink", "target": os.readlink(target)})
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
        return f"ERROR: trash failed: {err.strip() or f'exit {rc}'}"
    return None





def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _line_hash(line: str) -> str:
    return hashlib.sha1(line.encode("utf-8", errors="replace")).hexdigest()[:2]


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


def _image_marker(path: Path, mime: str, size: int) -> str:
    return f"{_IMAGE_RESULT_PREFIX}{path}\t{mime}\tVISUAL_FILE {path} mime={mime} size={size} bytes"


def _read_pdf_text(path: Path, context: ToolContext) -> tuple[str, bytes]:
    data = _read_regular_bytes(path)
    rc, out, err = run(["pdftotext", str(path), "-"], context)
    if rc == 0 and out.strip():
        return out[: context.max_tool_result_bytes], data
    detail = err.strip() or "no extractable text"
    return f"ERROR: pdftotext failed for {path}: {detail}", data


def _truncate_line(line: str, max_chars: int) -> str:
    if len(line) <= max_chars:
        return line
    return f"{line[:max_chars]}... [truncated, line exceeds {max_chars} chars]"



def _read_text(path: Path, context: ToolContext) -> tuple[str, bytes]:
    data = _read_regular_bytes(path)
    if len(data) > context.max_file_bytes:
        raise ValueError(
            f"File size ({len(data)} bytes) exceeds the maximum allowed size of {context.max_file_bytes} bytes"
        )
    if _is_binary(path):
        raise ValueError("Binary or visual files are not supported by this Python port yet")
    return data.decode("utf-8"), data


def _format_numbered_lines(lines: list[str], start_line: int, max_chars: int) -> str:
    out: list[str] = []
    for idx, line in enumerate(lines, start=start_line):
        truncated = _truncate_line(line, max_chars)
        out.append(f"{idx}{_line_hash(truncated)}|{truncated}")
    return "\n".join(out)


def _detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _normalize_line_endings(text: str, target: str) -> str:
    normalized = text.replace("\r\n", "\n")
    return normalized.replace("\n", target) if target == "\r\n" else normalized


def _write_bytes_preserving_existing_newlines(path: Path, content: str) -> bytes:
    line_ending = "\n"
    if path.exists():
        try:
            existing = path.read_text()
            line_ending = _detect_line_ending(existing)
        except UnicodeDecodeError:
            line_ending = "\n"
    normalized = _normalize_line_endings(content, line_ending)
    data = normalized.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def fs_read(
    path: str | None = None,
    file_path: str | None = None,
    range: dict | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    show_line_numbers: bool = True,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    raw_path = file_path or path
    if not raw_path:
        return "ERROR: file_path is required"
    if isinstance(range, dict):
        start_line = start_line if start_line is not None else range.get("start_line")
        end_line = end_line if end_line is not None else range.get("end_line")
    # A whole-file read (no range asked for) is the only one gated by
    # max_read_bytes. Once the caller names a range it is reading deliberately,
    # so a 40 MB log stays addressable line-by-line — max_file_bytes is still
    # the outer ceiling for both.
    # Matches the acceptance rule the line math below uses: bools, junk and
    # out-of-range values are not a range, so they can't smuggle a whole-file
    # read past the cap.
    ranged = (
        int_or_default(start_line, -1, minimum=1) != -1
        or int_or_default(end_line, -1, minimum=1) != -1
    )
    target = context.resolve_path(raw_path)
    if not target.exists():
        return f"ERROR: no such file: {target}"
    if not target.is_file():
        return f"ERROR: not a regular file: {target}"

    try:
        size = target.stat().st_size
        header = _read_regular_bytes(target, 16)
    except OSError as exc:
        return f"ERROR: {exc}"

    mime = _detect_visual_mime(target, header)
    if mime and mime.startswith("image/"):
        if size > context.max_file_bytes:
            return f"ERROR: image size ({size} bytes) exceeds the maximum allowed size of {context.max_file_bytes} bytes"
        try:
            data = _read_regular_bytes(target)
        except OSError as exc:
            return f"ERROR: {exc}"
        content_hash = _hash_bytes(data)
        context.remember_read(target, content_hash)
        if not context.vision_enabled:
            return _visual_fallback(target, mime, size)
        return _image_marker(target, mime, size)

    if mime == "application/pdf":
        try:
            text, data = _read_pdf_text(target, context)
        except OSError as exc:
            return f"ERROR: {exc}"
        content_hash = _hash_bytes(data)
        context.remember_read(target, content_hash)
        return text

    read_cap = int(getattr(context, "max_read_bytes", 0) or 0)
    if not ranged and read_cap > 0 and size > read_cap:
        return (
            f"ERROR: file size ({size} bytes) exceeds limits.max_read_bytes ({read_cap}) "
            f"for a whole-file read. Pass start_line/end_line to read a range instead — "
            f"ranged reads are not subject to this cap."
        )

    try:
        text, data = _read_text(target, context)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return f"ERROR: {exc}"

    content_hash = _hash_bytes(data)
    context.remember_read(target, content_hash)

    all_lines = text.splitlines()
    total = len(all_lines)
    if total == 0:
        return f"{target} is empty (hash {content_hash})"

    # Resolve the window the way forge's resolve_range does: a reversed range is
    # swapped, an oversized one is clamped to max_read_lines, and an omitted
    # end_line means "one page starting at start_line" — NOT "one page starting at
    # line 1", which used to make the tool's own
    # `read ... with start_line=N to continue` hint fail with an invalid-range error.
    raw_start = int_or_default(start_line, 1, minimum=1)
    raw_end = int_or_default(end_line, 0, minimum=1)
    if raw_end and raw_end < raw_start:
        raw_start, raw_end = raw_end, raw_start
    start = max(1, raw_start)
    if start > total:
        return f"{target} has {total} total lines; requested start_line={start} is past EOF (hash {content_hash})"
    page_end = start + context.max_read_lines - 1
    end = min(total, page_end, raw_end or page_end)

    selected = all_lines[start - 1:end]
    # .jsonl rows are single-line records that routinely exceed the normal cap;
    # give them a dedicated (larger) per-line budget so they are not truncated.
    max_chars = context.jsonl_max_line_chars if target.suffix.lower() == ".jsonl" else context.max_line_chars
    body = _format_numbered_lines(selected, start, max_chars) if show_line_numbers else "\n".join(selected)
    suffix = ""
    if end < total:
        suffix = f"\n[{total} total lines; read {target} with start_line={end + 1} to continue]"
    return f"{body}{suffix}"


read = fs_read


def write(file_path: str | None = None, content: str = "", overwrite: bool = False, context: ToolContext | None = None, path: str | None = None) -> str:
    assert context is not None
    raw_path = file_path or path
    if not raw_path:
        return "ERROR: file_path is required"
    target = context.resolve_path(raw_path)
    if target.exists() and not overwrite:
        return "ERROR: Cannot overwrite existing file: overwrite flag not set."
    if target.exists() and overwrite:
        guard = context.require_read(target, "overwrite it")
        if guard:
            return guard
    try:
        context.snapshot(target)
        data = _write_bytes_preserving_existing_newlines(target, content)
    except OSError as exc:
        return f"ERROR: {exc}"
    content_hash = _hash_bytes(data)
    context.file_hashes[target] = content_hash
    return f"wrote {len(data)} bytes to {target} (hash {content_hash})"


def remove(path: str, permanent: bool | None = False, context: ToolContext | None = None) -> str:
    assert context is not None
    target = _resolve_path_no_follow(context, path)
    if not target.exists() and not target.is_symlink():
        return f"ERROR: no such path: {target}"
    try:
        size = _path_size_no_follow(target)
        if not permanent and size > _TRASH_MAX_BYTES:
            return f"ERROR: target is over the 512 MiB trash limit ({size} bytes); confirm with KING and pass permanent=true to delete directly."
        _snapshot_remove_target(context, target)
        if not permanent:
            error = _trash_target(target, context)
            if error:
                context.snapshots.get(target, []).pop()
                return error
            return f"trashed {target}"
        _delete_target_no_follow(target)
    except OSError as exc:
        return f"ERROR: {exc}"
    return f"removed {target}"


def undo(path: str, context: ToolContext | None = None) -> str:
    assert context is not None
    # write/patch key snapshots under resolve_path (follows symlinks); remove keys
    # under the no-follow abspath. Try both so undo finds the snapshot either laid it.
    target = context.resolve_path(path)
    if not context.snapshots.get(target):
        no_follow = _resolve_path_no_follow(context, path)
        if context.snapshots.get(no_follow):
            target = no_follow
    stack = context.snapshots.get(target) or []
    if not stack:
        return f"ERROR: no snapshot available for {target}"
    previous = stack.pop()
    try:
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
            entries = previous.get("entries", {})
            for rel, data in entries.items():
                child = target / rel.rstrip("/")
                if rel.endswith("/"):
                    child.mkdir(parents=True, exist_ok=True)
                else:
                    child.parent.mkdir(parents=True, exist_ok=True)
                    child.write_bytes(data or b"")
            return f"restored directory {target}"
        if previous is None:
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            return f"restored deletion state for {target}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(previous)
        content_hash = _hash_bytes(previous)
        context.file_hashes[target] = content_hash
        return f"restored {target} (hash {content_hash})"
    except OSError as exc:
        return f"ERROR: {exc}"


def _normalize_edit(raw: object, label: str) -> tuple[str, str, bool] | str:
    """Validate one edit's *shape* — the part that does not depend on file content.

    Returns ``(old, new, replace_all)`` or an ERROR string. Runs for every edit
    before the file is touched, so a malformed batch is rejected without a read
    or a write. ``search``/``content`` are accepted as aliases here for the same
    reason they are accepted at the top level: models emit both spellings.
    """
    if not isinstance(raw, dict):
        return f"ERROR: {label}each edit must be an object with old_string and new_string"
    old = raw.get("old_string") if raw.get("old_string") is not None else raw.get("search")
    new = raw.get("new_string") if raw.get("new_string") is not None else raw.get("content")
    if old is None or new is None:
        return f"ERROR: {label}every edit requires old_string and new_string"
    old, new = str(old), str(new)
    if old == new:
        return f"ERROR: {label}old_string and new_string must be different"
    return old, new, bool(raw.get("replace_all", False))


def _apply_edit(text: str, old: str, new: str, replace_all: bool, label: str) -> tuple[str, int] | str:
    """Apply one exact replacement to ``text`` in memory. Returns the updated text
    and its match count, or an ERROR string. Line endings are normalised per edit
    against the text as it stands *now*, so a later edit sees an earlier one's result."""
    line_ending = _detect_line_ending(text)
    old_norm = _normalize_line_endings(old, line_ending)
    new_norm = _normalize_line_endings(new, line_ending)
    count = text.count(old_norm)
    if count == 0:
        return f"ERROR: {label}Could not find match for search text: {old!r}. File may have changed externally, consider reading the file again."
    if count > 1 and not replace_all:
        return f"ERROR: {label}Multiple matches found for search text: {old!r}. Either provide a more specific search pattern or use replace_all."
    return text.replace(old_norm, new_norm, -1 if replace_all else 1), count


def patch(
    path: str | None = None,
    file_path: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    search: str | None = None,
    content: str | None = None,
    replace_all: bool = False,
    edits: list[dict] | None = None,
    context: ToolContext | None = None,
) -> str:
    """Exact string replacement in one file, in one of two forms.

    Scalar (the common case)::

        patch(file_path, old_string, new_string, replace_all=False)

    Batch — several replacements applied in order, each seeing the previous
    one's result::

        patch(file_path, edits=[{old_string, new_string, replace_all}, ...])

    Both forms are ONE code path: same read-before-edit guard, same match rules
    (must exist, must be unique unless replace_all, must differ from new_string),
    one snapshot, one write. Batch validation is all-or-nothing: every edit is
    validated and applied in memory first, so a miss, an ambiguity or a no-op
    anywhere in the list writes nothing and names the offending edit. The final
    filesystem write is not crash-atomic. Mixing the two forms is an error, never
    a silent precedence rule.
    """
    assert context is not None
    raw_path = file_path or path
    if not raw_path:
        return "ERROR: path is required"

    old = old_string if old_string is not None else search
    new = new_string if new_string is not None else content
    batch = edits is not None

    if batch and (old is not None or new is not None or replace_all):
        return (
            "ERROR: pass either the scalar form (old_string/new_string/replace_all) "
            "or edits, not both"
        )

    if not batch:
        if old is None or new is None:
            return "ERROR: old_string and new_string are required"
        single = _normalize_edit({"old_string": old, "new_string": new, "replace_all": replace_all}, "")
        if isinstance(single, str):
            return single
        pending = [single]
    else:
        if not isinstance(edits, list) or not edits:
            return "ERROR: edits must be a non-empty list of {old_string, new_string} objects"
        pending = []
        for index, raw in enumerate(edits, start=1):
            normalized = _normalize_edit(raw, f"edit {index}: ")
            if isinstance(normalized, str):
                return normalized
            pending.append(normalized)

    target = context.resolve_path(raw_path)
    guard = context.require_read(target, "edit it")
    if guard:
        return guard
    try:
        source = target.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        return f"ERROR: {exc}"

    updated = source
    replacements = 0
    for index, (old_text, new_text, edit_replace_all) in enumerate(pending, start=1):
        applied = _apply_edit(updated, old_text, new_text, edit_replace_all, f"edit {index}: " if batch else "")
        if isinstance(applied, str):
            return applied
        updated, count = applied
        replacements += count if edit_replace_all else 1

    context.snapshot(target)
    target.write_text(updated)
    data = updated.encode("utf-8")
    content_hash = _hash_bytes(data)
    context.file_hashes[target] = content_hash
    diff = "".join(difflib.unified_diff(source.splitlines(True), updated.splitlines(True), fromfile=str(target), tofile=str(target)))
    if len(diff) > 4000:
        diff = diff[:4000] + "\n... [diff truncated]"
    if batch:
        summary = f"{len(pending)} edit{'s' if len(pending) != 1 else ''}"
    else:
        summary = f"{replacements} replacement{'s' if replacements != 1 else ''}"
    return f"patched {target} ({summary}, hash {content_hash})\n{diff}"


def _iter_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules", ".venv", "venv"}]
        for name in files:
            candidate = Path(base) / name
            # Regular files only: a FIFO/socket/device (or a symlink to one) in
            # the walk would otherwise reach a reader and, for a pipe with no
            # writer, hang the whole turn on open(). is_file() stats (never
            # opens) and follows symlinks, so only true regular files pass.
            if candidate.is_file():
                yield candidate


_RG_MISSING = "ERROR: rg (ripgrep) not found in js/tools or PATH; run `just install` to provision it."
_RG_TIMEOUT_S = 120
_AST_GREP_BINARY = "/home/ronald_rump/.local/bin/ast-grep"
_AST_GREP_TIMEOUT_S = 120
_AST_GREP_LANGUAGES = (
    "Bash", "C", "Cpp", "CSharp", "Css", "Dart", "Elixir", "Go",
    "Haskell", "Hcl", "Html", "Java", "JavaScript", "Json", "Kotlin",
    "Lua", "Markdown", "Nix", "Php", "Python", "Ruby", "Rust", "Scala",
    "Solidity", "Swift", "Tsx", "TypeScript", "Yaml",
)


def _rg_binary() -> str | None:
    return resolve_binary("rg")


def _rg_env() -> dict[str, str]:
    # Inherit the real environment (PATH, locale) but drop the box-local ripgrep
    # config so the documented contract holds everywhere: .gitignore honoured
    # inside a git tree, .ignore/.rgignore anywhere, hidden + binary + non-regular
    # files skipped. A stray RIPGREP_CONFIG_PATH must not silently change results.
    env = dict(os.environ)
    env.pop("RIPGREP_CONFIG_PATH", None)
    return env


@lru_cache(maxsize=1)
def _rg_types(rg: str) -> frozenset[str]:
    """Names rg accepts for `--type` (`rust`, `py`, `js`, ...), from `rg --type-list`."""
    try:
        proc = subprocess.run(
            [rg, "--type-list"], capture_output=True, text=True, timeout=10, env=_rg_env()
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(
        line.split(":", 1)[0].strip() for line in proc.stdout.splitlines() if ":" in line
    )


def _rg_stream(
    argv: list[str], want: int, timeout: int, input_text: str | None = None
) -> tuple[list[str], int | None, str, bool]:
    """Run rg and collect at most *want* output lines, then stop it.

    Returns (lines, returncode, stderr, timed_out). returncode is None when rg
    was stopped early because enough lines were already gathered — the caller
    treats that as a successful match. Streaming with an early stop keeps memory
    bounded (a pattern matching millions of lines never buffers them all).

    The deadline is enforced by a watchdog that kills rg, NOT by a check inside
    the read loop: `for line in proc.stdout` blocks until rg emits something, so a
    loop-side check can only fire while output is already flowing — exactly the
    case that does not need a timeout. A long scan that matches nothing emits no
    lines at all, and used to run unbounded.

    The child gets its own session so the watchdog can signal the whole group.
    Killing only the direct child leaves any grandchild holding the write end of
    the pipe, and the read loop then blocks past the deadline anyway — the exact
    hang the watchdog exists to prevent."""
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_rg_env(), text=True, encoding="utf-8", errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return [], 2, str(exc), False
    lines: list[str] = []
    stopped_early = False
    expired = threading.Event()

    def _kill_group() -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, AttributeError):
            proc.kill()

    def _expire() -> None:
        expired.set()
        _kill_group()

    watchdog = threading.Timer(timeout, _expire)
    watchdog.daemon = True
    watchdog.start()
    writer: threading.Thread | None = None
    if input_text is not None:
        assert proc.stdin is not None

        def write_stdin() -> None:
            try:
                proc.stdin.write(input_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        writer = threading.Thread(target=write_stdin, daemon=True, name="search-stdin")
        writer.start()
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            if len(lines) >= want:
                stopped_early = True
                break
    finally:
        watchdog.cancel()
        if stopped_early:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_group()
                proc.wait()
    timed_out = expired.is_set()
    stderr = proc.stderr.read() if proc.stderr is not None else ""
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    if not stopped_early and proc.poll() is None:
        proc.wait()
    if writer is not None:
        writer.join(timeout=1)
    rc = None if stopped_early else proc.returncode
    return lines, rc, stderr, timed_out


def fs_search(
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    output_mode: str | None = None,
    before_context: int | None = None,
    after_context: int | None = None,
    context_lines: int | None = None,
    show_line_numbers: bool | None = True,
    case_insensitive: bool | None = False,
    file_type: str | None = None,
    head_limit: int | None = None,
    offset: int | None = None,
    multiline: bool | None = False,
    context: ToolContext | None = None,
    **rg_flags,
) -> str:
    assert context is not None
    before_context = rg_flags.get("-B", before_context)
    after_context = rg_flags.get("-A", after_context)
    context_lines = rg_flags.get("-C", context_lines)
    show_line_numbers = rg_flags.get("-n", show_line_numbers)
    case_insensitive = rg_flags.get("-i", case_insensitive)
    file_type = rg_flags.get("type", file_type)
    root = context.resolve_path(path or ".")
    if not root.exists():
        return f"ERROR: Path does not exist: {root}"
    try:
        root_stat = root.stat()
    except OSError as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"
    if not stat.S_ISDIR(root_stat.st_mode) and not stat.S_ISREG(root_stat.st_mode):
        return f"ERROR: not a regular file or directory: {root}"
    mode = output_mode or "files_with_matches"
    cache_key = repr((pattern, str(root), glob, mode, before_context, after_context, context_lines, show_line_numbers, case_insensitive, file_type, head_limit, offset, multiline))
    if cache_key in context.search_cache:
        return context.search_cache[cache_key] + "\n[deduplicated repeated search]"

    rg = _rg_binary()
    if rg is None:
        return _RG_MISSING

    skip = int_or_default(offset, 0, minimum=0)
    limit = int_or_default(head_limit, 10_000, minimum=1)
    before = int_or_default(context_lines if context_lines is not None else before_context, 0, minimum=0)
    after = int_or_default(context_lines if context_lines is not None else after_context, 0, minimum=0)

    argv = [rg, "--color=never", "--no-messages"]
    if mode == "files":
        argv.append("--files")
    elif mode == "files_with_matches":
        argv.append("--files-with-matches")
    elif mode == "count":
        argv.append("--count")
    elif mode == "content":
        argv += ["--no-heading", "--with-filename"]
        argv.append("--line-number" if show_line_numbers else "--no-line-number")
        if before:
            argv += ["--before-context", str(before)]
        if after:
            argv += ["--after-context", str(after)]
    else:
        return "ERROR: output_mode must be one of files, files_with_matches, content, count"
    if case_insensitive and mode != "files":
        argv.append("--ignore-case")
    if multiline:
        argv += ["--multiline", "--multiline-dotall"]
    glob_flag = "--iglob" if case_insensitive and mode == "files" else "--glob"
    if glob:
        argv += [glob_flag, str(glob)]
    if file_type:
        # Prefer rg's own type table so `type=rust` matches *.rs (and `py` also
        # matches .pyi/.pyw), matching forge's `--type` behaviour. A bare extension
        # rg has no type for ("gdshader") still works via an extension glob —
        # previously EVERY value became `*.<value>`, so `type=rust` silently matched
        # nothing at all.
        name = str(file_type).strip().lstrip(".")
        if name in _rg_types(rg):
            argv += ["--type", name]
        else:
            argv += ["--glob", f"*.{name}"]
    if mode == "files":
        argv += [glob_flag, pattern, "--", str(root)]
    else:
        argv += ["--regexp", pattern, "--", str(root)]

    lines, rc, stderr, timed_out = _rg_stream(argv, skip + limit, _RG_TIMEOUT_S)
    if timed_out:
        return f"ERROR: search timed out after {_RG_TIMEOUT_S}s"
    # rc None = rg stopped early with a full page of matches; 0 = matches; 1 = no
    # matches (clean empty result); anything else = real rg error (bad regex/glob).
    if rc is not None and rc not in (0, 1):
        detail = (stderr or "").strip()
        first = detail.splitlines()[0] if detail else f"rg exit {rc}"
        return f"ERROR: {first}"

    sliced = lines[skip:skip + limit]
    out = "\n".join(sliced) if sliced else "(no matches)"
    context.search_cache[cache_key] = out
    return out


def _ast_language(raw: str | None) -> str | None:
    if raw is None:
        return None
    folded = str(raw).strip().casefold()
    return next((language for language in _AST_GREP_LANGUAGES if language.casefold() == folded), "")


def _ast_argv(
    pattern: str,
    root: Path,
    lang: str | None,
    rewrite: str | None = None,
    stdin: bool = False,
) -> list[str]:
    argv = [
        _AST_GREP_BINARY,
        "run",
        "--pattern",
        pattern,
        "--json=stream",
        "--color",
        "never",
    ]
    if lang:
        argv += ["--lang", lang]
    if rewrite is not None:
        argv += ["--rewrite", rewrite]
    if stdin:
        argv.append("--stdin")
    else:
        argv += ["--", str(root)]
    return argv


def _ast_records(
    pattern: str,
    root: Path,
    lang: str | None,
    rewrite: str | None,
    want: int,
) -> tuple[list[dict], int | None, str, bool, str | None, bool]:
    lines, rc, stderr, timed_out = _rg_stream(
        _ast_argv(pattern, root, lang, rewrite), want, _AST_GREP_TIMEOUT_S
    )

    used_stdin = False
    if rc == 1 and not lines and lang and root.is_file():
        try:
            source = _read_regular_bytes(root).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return [], rc, stderr, timed_out, str(exc), used_stdin
        lines, rc, stderr, timed_out = _rg_stream(
            _ast_argv(pattern, root, lang, rewrite, stdin=True),
            want,
            _AST_GREP_TIMEOUT_S,
            input_text=source,
        )
        used_stdin = True

    records: list[dict] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            return [], rc, stderr, timed_out, f"invalid ast-grep JSON output: {exc.msg}", used_stdin
        if not isinstance(record, dict):
            return [], rc, stderr, timed_out, "invalid ast-grep JSON output: expected an object", used_stdin
        if used_stdin and record.get("file") == "STDIN":
            record["file"] = str(root)
        records.append(record)
    return records, rc, stderr, timed_out, None, used_stdin


def _cap_ast_output(text: str, context: ToolContext) -> str:
    cap = max(0, int(context.max_tool_result_bytes))
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text
    marker = f"[truncated: limits.max_tool_result_bytes ({cap}) reached]"
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= cap:
        return marker_bytes[:cap].decode("utf-8", errors="ignore")
    prefix = encoded[: cap - len(marker_bytes) - 1].decode("utf-8", errors="ignore")
    return f"{prefix}\n{marker}"


def _ast_match_output(records: list[dict], context: ToolContext) -> str:
    blocks: list[str] = []
    for record in records:
        raw_file = record.get("file")
        raw_range = record.get("range")
        if not isinstance(raw_file, str) or not isinstance(raw_range, dict):
            return "ERROR: invalid ast-grep JSON output: match has no file or range"
        start = raw_range.get("start")
        if not isinstance(start, dict) or not isinstance(start.get("line"), int):
            return "ERROR: invalid ast-grep JSON output: match has no start line"
        target = context.resolve_path(raw_file)
        line_number = start["line"] + 1
        source = record.get("lines", record.get("text", ""))
        if not isinstance(source, str):
            return "ERROR: invalid ast-grep JSON output: match has no source text"
        source_lines = source.splitlines() or [""]
        numbered = _format_numbered_lines(source_lines, line_number, context.max_line_chars)
        blocks.append(f"{target}:{line_number}\n{numbered}")
    return "\n".join(blocks) if blocks else "(no matches)"


def _prepare_ast_rewrite(
    records: list[dict], context: ToolContext
) -> dict[Path, tuple[bytes, bytes]] | str:
    edits_by_file: dict[Path, list[tuple[int, int, bytes, bytes]]] = {}
    for record in records:
        raw_file = record.get("file")
        offsets = record.get("replacementOffsets")
        replacement = record.get("replacement")
        text = record.get("text")
        if (
            not isinstance(raw_file, str)
            or not isinstance(offsets, dict)
            or not isinstance(offsets.get("start"), int)
            or not isinstance(offsets.get("end"), int)
            or not isinstance(replacement, str)
            or not isinstance(text, str)
        ):
            return "ERROR: invalid ast-grep JSON output: rewrite metadata is incomplete"
        target = context.resolve_path(raw_file)
        edits_by_file.setdefault(target, []).append(
            (offsets["start"], offsets["end"], text.encode("utf-8"), replacement.encode("utf-8"))
        )

    prepared: dict[Path, tuple[bytes, bytes]] = {}
    for target, edits in edits_by_file.items():
        try:
            source = target.read_bytes()
        except OSError as exc:
            return f"ERROR: {exc}"
        previous_end = -1
        for start, end, expected, _replacement in sorted(edits):
            if start < previous_end:
                return f"ERROR: ast-grep returned overlapping rewrites for {target}"
            if start < 0 or end < start or source[start:end] != expected:
                return f"ERROR: {target} changed while preparing the structural rewrite"
            previous_end = end
        updated = source
        for start, end, _expected, replacement in sorted(edits, reverse=True):
            updated = updated[:start] + replacement + updated[end:]
        prepared[target] = (source, updated)
    return prepared


def _ast_rewrite_diff(prepared: dict[Path, tuple[bytes, bytes]]) -> str:
    chunks: list[str] = []
    for target, (source, updated) in prepared.items():
        chunks.append(
            "".join(
                difflib.unified_diff(
                    source.decode("utf-8", errors="replace").splitlines(keepends=True),
                    updated.decode("utf-8", errors="replace").splitlines(keepends=True),
                    fromfile=str(target),
                    tofile=str(target),
                )
            )
        )
    return "".join(chunks).rstrip()


def ast_search(
    pattern: str,
    path: str | None = None,
    lang: str | None = None,
    rewrite: str | None = None,
    apply: bool = False,
    max_results: int | None = None,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    if not isinstance(pattern, str) or not pattern.strip():
        return "ERROR: pattern is required"
    if rewrite is not None and not isinstance(rewrite, str):
        return "ERROR: rewrite must be a string"
    if apply and rewrite is None:
        return "ERROR: apply=true requires rewrite"

    language = _ast_language(lang)
    if language == "":
        return f"ERROR: unsupported ast-grep language: {lang}"
    root = context.resolve_path(path or ".")
    if not root.exists():
        return f"ERROR: Path does not exist: {root}"
    try:
        root_stat = root.stat()
    except OSError as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"
    if not stat.S_ISDIR(root_stat.st_mode) and not stat.S_ISREG(root_stat.st_mode):
        return f"ERROR: not a regular file or directory: {root}"

    limit = int_or_default(max_results, 100, minimum=1)
    cache_key = repr(("ast_search", pattern, str(root), language, rewrite, limit))
    if not apply and cache_key in context.search_cache:
        return context.search_cache[cache_key] + "\n[deduplicated repeated search]"

    records, rc, stderr, timed_out, parse_error, used_stdin = _ast_records(
        pattern, root, language, rewrite, limit + 1
    )
    if timed_out:
        return f"ERROR: search timed out after {_AST_GREP_TIMEOUT_S}s"
    if parse_error:
        return f"ERROR: {parse_error}"
    if rc is not None and rc not in (0, 1):
        detail = stderr.strip()
        first = detail.splitlines()[0] if detail else f"ast-grep exit {rc}"
        return f"ERROR: {first}"

    overflow = len(records) > limit
    visible = records[:limit]
    if rewrite is None:
        out = _ast_match_output(visible, context)
        out = _cap_ast_output(out, context)
        context.search_cache[cache_key] = out
        return out

    prepared = _prepare_ast_rewrite(visible, context)
    if isinstance(prepared, str):
        return prepared
    diff = _ast_rewrite_diff(prepared) or "(no changes)"
    if not apply:
        suffix = "\n[additional matches omitted; increase max_results to preview them]" if overflow else ""
        out = f"DRY RUN: no files changed. Pass apply=true to apply.\n{diff}{suffix}"
        out = _cap_ast_output(out, context)
        context.search_cache[cache_key] = out
        return out
    if overflow:
        return (
            f"ERROR: rewrite matched more than max_results={limit}; "
            "narrow path or increase max_results before applying"
        )
    if not prepared or all(source == updated for source, updated in prepared.values()):
        return "(no changes)"

    for target in prepared:
        context.snapshot(target)
    apply_stderr = ""
    if used_stdin:
        rc = 0
        for target, (_source, updated) in prepared.items():
            try:
                target.write_bytes(updated)
            except OSError as exc:
                rc = 1
                apply_stderr = str(exc)
                break
    else:
        argv = _ast_argv(pattern, root, language, rewrite)
        argv.remove("--json=stream")
        argv.insert(-2, "--update-all")
        rc, _stdout, apply_stderr = run(argv, context=context, timeout=_AST_GREP_TIMEOUT_S)
    context.search_cache.clear()
    if rc != 0:
        detail = apply_stderr.strip()
        first = detail.splitlines()[0] if detail else f"ast-grep exit {rc}"
        return f"ERROR: {first}; snapshots are available through undo"

    changed = 0
    for target, (source, _predicted) in prepared.items():
        try:
            updated = target.read_bytes()
        except OSError as exc:
            return f"ERROR: {exc}; snapshots are available through undo"
        if updated != source:
            changed += 1
        context.file_hashes[target] = _hash_bytes(updated)
        prepared[target] = (source, updated)
    diff = _ast_rewrite_diff(prepared) or "(no changes)"
    summary = f"rewrote {len(visible)} match{'es' if len(visible) != 1 else ''} in {changed} file{'s' if changed != 1 else ''}"
    return _cap_ast_output(f"{summary}\n{diff}", context)


def list_dir(path: str, recursive: bool = False, context: ToolContext | None = None) -> str:
    assert context is not None
    root = context.resolve_path(path)
    if not root.exists():
        return f"ERROR: no such path: {root}"
    if not root.is_dir():
        return f"ERROR: not a directory: {root}"
    lines: list[str] = []
    if recursive:
        for p in _iter_files(root):
            rel = p.relative_to(root)
            lines.append(f"  {rel} ({p.stat().st_size}b)")
    else:
        for entry in sorted(root.iterdir()):
            marker = "/" if entry.is_dir() else ""
            size = "" if entry.is_dir() else f" ({entry.stat().st_size}b)"
            lines.append(f"  {entry.name}{marker}{size}")
    return f"{root}:\n" + ("\n".join(lines) if lines else "  (empty)")



def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "read",
            load_description("read"),
            fs_read,
            {
                "file_path": {"type": "string", "description": "Absolute, relative, or ~ path to a file."},
                "range": {
                    "type": "object",
                    "properties": {
                        "start_line": {"type": "integer", "description": "Optional 1-based first line for text files."},
                        "end_line": {"type": "integer", "description": "Optional inclusive 1-based last line for text files."},
                    },
                    "additionalProperties": False,
                    "description": "Optional line range for partial reads.",
                },
                "show_line_numbers": {"type": "boolean", "default": True, "description": "For text output, prefix each line with its anchored line number."},
            },
            required=("file_path",),
        ),
        Tool(
            "write",
            load_description("write"),
            write,
            {
                "file_path": {"type": "string", "description": "File path to create or overwrite."},
                "content": {"type": "string", "description": "Complete file content to write."},
                "overwrite": {"type": "boolean", "default": False, "description": "Required for existing files after reading them first."},
            },
            required=("file_path", "content"),
        ),
        Tool(
            "fs_search",
            load_description("fs_search"),
            fs_search,
            {
                "pattern": {"type": "string", "description": "Content regular expression, or filename glob when output_mode is files."},
                "path": {"type": "string", "description": "File or directory to search; defaults to the current working directory."},
                "glob": {"type": "string", "description": "Optional glob filter such as *.py or **/*.tsx."},
                "output_mode": {"type": "string", "enum": ["files", "content", "files_with_matches", "count"], "default": "files_with_matches", "description": "Result format: filename-glob paths, content-matching lines, paths whose contents match, or per-file content-match counts."},
                # Both spellings are declared because the handler has always accepted
                # both and the description teaches the readable ones: with only the
                # rg-style keys declared under additionalProperties:false, a model
                # following the prose emitted arguments its own schema rejected.
                "-B": {"type": "integer", "description": "Lines before each match when output_mode is content (alias: before_context)."},
                "before_context": {"type": "integer", "description": "Lines before each match when output_mode is content."},
                "-A": {"type": "integer", "description": "Lines after each match when output_mode is content (alias: after_context)."},
                "after_context": {"type": "integer", "description": "Lines after each match when output_mode is content."},
                "-C": {"type": "integer", "description": "Lines before and after each match when output_mode is content (alias: context_lines)."},
                "context_lines": {"type": "integer", "description": "Lines before and after each match when output_mode is content."},
                "-n": {"type": "boolean", "default": True, "description": "Include file:line prefixes for content output (alias: show_line_numbers)."},
                "show_line_numbers": {"type": "boolean", "default": True, "description": "Include file:line prefixes for content output."},
                "-i": {"type": "boolean", "default": False, "description": "Match without case sensitivity (alias: case_insensitive)."},
                "case_insensitive": {"type": "boolean", "default": False, "description": "Match without case sensitivity."},
                "type": {"type": "string", "description": "ripgrep type name or bare extension, e.g. rust, py, rs (alias: file_type)."},
                "file_type": {"type": "string", "description": "ripgrep type name or bare extension, e.g. rust, py, rs."},
                "head_limit": {"type": "integer", "description": "Maximum number of result entries after offset."},
                "offset": {"type": "integer", "description": "Number of result entries to skip before returning output."},
                "multiline": {"type": "boolean", "default": False, "description": "Allow the regex to span line breaks."},
            },
            required=("pattern",),
        ),
        Tool(
            "ast_search",
            load_description("ast_search"),
            ast_search,
            {
                "pattern": {"type": "string", "description": "ast-grep structural pattern with metavariables such as $NAME and $$$ARGS."},
                "path": {"type": "string", "description": "File or directory to search; defaults to the current working directory."},
                "lang": {"type": "string", "enum": list(_AST_GREP_LANGUAGES), "description": "Optional language override; source extensions are inferred when omitted."},
                "rewrite": {"type": "string", "description": "Optional structural replacement. Supplying it produces a dry-run diff unless apply is true."},
                "apply": {"type": "boolean", "default": False, "description": "Apply the rewrite to files. False is a non-mutating dry run."},
                "max_results": {"type": "integer", "default": 100, "description": "Maximum matches returned or rewritten; an apply is refused when more matches exist."},
            },
            required=("pattern",),
        ),
        Tool("remove", load_description("remove"), remove, {"path": {"type": "string", "description": "File or directory path to delete."}, "permanent": {"type": "boolean", "default": False, "description": "Delete directly after KING confirms permanent deletion."}}, required=("path",)),
        Tool(
            "patch",
            load_description("patch"),
            patch,
            {
                "file_path": {"type": "string", "description": "File path to edit."},
                "old_string": {"type": "string", "description": "Exact text to replace. Required unless edits is used."},
                "new_string": {"type": "string", "description": "Replacement text; must differ from old_string. Required unless edits is used."},
                "replace_all": {"type": "boolean", "default": False, "description": "Replace every occurrence instead of requiring one unique match."},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string", "description": "Exact text to replace."},
                            "new_string": {"type": "string", "description": "Replacement text; must differ from old_string."},
                            "replace_all": {"type": "boolean", "default": False, "description": "Replace every occurrence of this edit."},
                        },
                        "required": ["old_string", "new_string"],
                        "additionalProperties": False,
                    },
                    "description": "Several replacements validated and applied in order before one final write. Use instead of old_string/new_string, never alongside them.",
                },
            },
            input_schema={
                "type": "object",
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "File path to edit."},
                            "old_string": {"type": "string", "description": "Exact text to replace."},
                            "new_string": {"type": "string", "description": "Replacement text; must differ from old_string."},
                            "replace_all": {"type": "boolean", "default": False, "description": "Replace every occurrence instead of requiring one unique match."},
                        },
                        "required": ["file_path", "old_string", "new_string"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "File path to edit."},
                            "edits": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "old_string": {"type": "string", "description": "Exact text to replace."},
                                        "new_string": {"type": "string", "description": "Replacement text; must differ from old_string."},
                                        "replace_all": {"type": "boolean", "default": False, "description": "Replace every occurrence of this edit."},
                                    },
                                    "required": ["old_string", "new_string"],
                                    "additionalProperties": False,
                                },
                                "description": "Several replacements validated and applied in order before one final write.",
                            },
                        },
                        "required": ["file_path", "edits"],
                        "additionalProperties": False,
                    },
                ],
            },
        ),
        Tool("undo", load_description("undo"), undo, {"path": {"type": "string", "description": "Path whose latest in-process snapshot should be restored."}}, required=("path",)),
    )
