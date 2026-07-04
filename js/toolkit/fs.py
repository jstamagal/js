"""Filesystem tools with read-before-write safety rails."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from collections.abc import Iterable

from .core import Tool, ToolContext
from .sanitize import int_or_default, text_or_default
from .descriptions import load_description
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





_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from",
    "how", "in", "into", "is", "it", "of", "on", "or", "that", "the", "this",
    "to", "use", "with", "where", "who", "why",
}

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

    start = max(1, int_or_default(start_line, 1, minimum=1))
    end = min(total, int_or_default(end_line, min(total, context.max_read_lines), minimum=1))
    if start > total:
        return f"{target} has {total} total lines; requested start_line={start} is past EOF (hash {content_hash})"
    if end < start:
        return f"ERROR: invalid line range {start}-{end}"
    if end - start + 1 > context.max_read_lines:
        return f"ERROR: range exceeds maximum of {context.max_read_lines} lines"

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


def patch(
    path: str | None = None,
    file_path: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    search: str | None = None,
    content: str | None = None,
    replace_all: bool = False,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    raw_path = file_path or path
    if not raw_path:
        return "ERROR: path is required"
    old = old_string if old_string is not None else search
    new = new_string if new_string is not None else content
    if old is None or new is None:
        return "ERROR: old_string and new_string are required"
    if old == new:
        return "ERROR: old_string and new_string must be different"

    target = context.resolve_path(raw_path)
    guard = context.require_read(target, "edit it")
    if guard:
        return guard
    try:
        source = target.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        return f"ERROR: {exc}"

    line_ending = _detect_line_ending(source)
    old_norm = _normalize_line_endings(old, line_ending)
    new_norm = _normalize_line_endings(new, line_ending)
    count = source.count(old_norm)
    if count == 0:
        return f"ERROR: Could not find match for search text: {old!r}. File may have changed externally, consider reading the file again."
    if count > 1 and not replace_all:
        return f"ERROR: Multiple matches found for search text: {old!r}. Either provide a more specific search pattern or use replace_all."

    updated = source.replace(old_norm, new_norm, -1 if replace_all else 1)
    context.snapshot(target)
    target.write_text(updated)
    data = updated.encode("utf-8")
    content_hash = _hash_bytes(data)
    context.file_hashes[target] = content_hash
    diff = "".join(difflib.unified_diff(source.splitlines(True), updated.splitlines(True), fromfile=str(target), tofile=str(target)))
    if len(diff) > 4000:
        diff = diff[:4000] + "\n... [diff truncated]"
    return f"patched {target} ({count if replace_all else 1} replacement{'s' if replace_all and count != 1 else ''}, hash {content_hash})\n{diff}"


def multi_patch(path: str | None = None, file_path: str | None = None, edits: list[dict] | None = None, context: ToolContext | None = None) -> str:
    assert context is not None
    raw_path = file_path or path
    if not raw_path:
        return "ERROR: path is required"
    target = context.resolve_path(raw_path)
    guard = context.require_read(target, "edit it")
    if guard:
        return guard
    edits = edits or []
    try:
        source = target.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        return f"ERROR: {exc}"
    updated = source
    for edit in edits:
        old = edit.get("old_string")
        new = edit.get("new_string")
        replace_all = bool(edit.get("replace_all", False))
        if old is None or new is None:
            return "ERROR: every edit requires old_string and new_string"
        line_ending = _detect_line_ending(updated)
        old_norm = _normalize_line_endings(str(old), line_ending)
        new_norm = _normalize_line_endings(str(new), line_ending)
        count = updated.count(old_norm)
        if count == 0:
            return f"ERROR: Could not find match for search text: {old!r}. File may have changed externally, consider reading the file again."
        if count > 1 and not replace_all:
            return f"ERROR: Multiple matches found for search text: {old!r}. Either provide a more specific search pattern or use replace_all."
        updated = updated.replace(old_norm, new_norm, -1 if replace_all else 1)
    context.snapshot(target)
    target.write_text(updated)
    data = updated.encode("utf-8")
    content_hash = _hash_bytes(data)
    context.file_hashes[target] = content_hash
    diff = "".join(difflib.unified_diff(source.splitlines(True), updated.splitlines(True), fromfile=str(target), tofile=str(target)))
    if len(diff) > 4000:
        diff = diff[:4000] + "\n... [diff truncated]"
    return f"patched {target} ({len(edits)} edits, hash {content_hash})\n{diff}"


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


_RG_MISSING = "ERROR: rg (ripgrep) not found on PATH; run `just install` to provision it."
_RG_TIMEOUT_S = 120


def _rg_binary() -> str | None:
    return shutil.which("rg")


def _rg_env() -> dict[str, str]:
    # Inherit the real environment (PATH, locale) but drop the box-local ripgrep
    # config so the documented contract holds everywhere: .gitignore honoured
    # inside a git tree, .ignore/.rgignore anywhere, hidden + binary + non-regular
    # files skipped. A stray RIPGREP_CONFIG_PATH must not silently change results.
    env = dict(os.environ)
    env.pop("RIPGREP_CONFIG_PATH", None)
    return env


def _rg_stream(argv: list[str], want: int, timeout: int) -> tuple[list[str], int | None, str, bool]:
    """Run rg and collect at most *want* output lines, then stop it.

    Returns (lines, returncode, stderr, timed_out). returncode is None when rg
    was stopped early because enough lines were already gathered — the caller
    treats that as a successful match. Streaming with an early stop keeps memory
    bounded (a pattern matching millions of lines never buffers them all) and,
    with the wall-clock deadline, guarantees the turn cannot hang."""
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_rg_env(), text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        return [], 2, str(exc), False
    lines: list[str] = []
    stopped_early = False
    timed_out = False
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            if len(lines) >= want:
                stopped_early = True
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
    finally:
        if stopped_early or timed_out:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    stderr = proc.stderr.read() if proc.stderr is not None else ""
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    if not stopped_early and proc.poll() is None:
        proc.wait()
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
    if mode == "files_with_matches":
        argv.append("--files-with-matches")
    elif mode == "count":
        argv.append("--count")
    else:  # content
        argv += ["--no-heading", "--with-filename"]
        argv.append("--line-number" if show_line_numbers else "--no-line-number")
        if before:
            argv += ["--before-context", str(before)]
        if after:
            argv += ["--after-context", str(after)]
    if case_insensitive:
        argv.append("--ignore-case")
    if multiline:
        argv += ["--multiline", "--multiline-dotall"]
    if glob:
        argv += ["--glob", str(glob)]
    if file_type:
        argv += ["--glob", f"*.{str(file_type).lstrip('.')}"]
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



def _camel_parts(value: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)


def _search_terms(*parts: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for part in parts:
        for raw in re.findall(r"[A-Za-z0-9_]+", _camel_parts(part).lower()):
            token = raw.strip("_")
            if len(token) < 2 or token in _STOP_WORDS or token in seen:
                continue
            seen.add(token)
            terms.append(token)
    return terms


def _query_parts(item: dict | str) -> tuple[str, str, str | None, str | None, int]:
    if isinstance(item, dict):
        query = text_or_default(item.get("query") or item.get("text") or item.get("q"))
        use_case = text_or_default(item.get("use_case") or item.get("useCase"))
        path = text_or_default(item.get("path") or item.get("root"))
        glob = text_or_default(item.get("glob") or item.get("file_pattern"))
        limit = int_or_default(item.get("limit") or item.get("top_k") or item.get("topK"), 10, minimum=1)
        return query, use_case, path or None, glob or None, max(1, min(limit, 50))
    return text_or_default(item), "", None, None, 10


def _line_score(line_lower: str, rel_lower: str, query_lower: str, use_case_lower: str, terms: list[str]) -> int:
    score = 0
    if query_lower and query_lower in line_lower:
        score += 80
    if use_case_lower and use_case_lower in line_lower:
        score += 30
    matched = 0
    for term in terms:
        count = line_lower.count(term)
        if count:
            matched += 1
            score += min(count, 4) * 8
            if re.search(rf"\b{re.escape(term)}\b", line_lower):
                score += 6
            if term in rel_lower:
                score += 5
    if matched:
        score += matched * matched * 3
        if matched == len(terms):
            score += 25
    if line_lower.lstrip().startswith(("def ", "class ", "function ", "const ", "let ", "type ", "interface ", "struct ", "enum ")):
        score += 5
    return score



def sem_search(queries: list[dict] | list[str], context: ToolContext | None = None) -> str:
    assert context is not None
    if not queries:
        return "ERROR: sem_search requires at least one query"

    sections: list[str] = [
        "Local semantic-ish search (term-ranked; no embeddings or external index):"
    ]
    output_bytes = sum(len(line.encode("utf-8")) + 1 for line in sections)
    budget = context.max_tool_result_bytes

    for query_index, item in enumerate(queries, 1):
        query, use_case, raw_path, glob, limit = _query_parts(item)
        terms = _search_terms(query, use_case)
        title = query or use_case
        if not terms:
            sections.append(f"\nQuery {query_index}: {title!r} -> ERROR: no searchable terms")
            continue

        root = context.resolve_path(raw_path or ".")
        if not root.exists():
            sections.append(f"\nQuery {query_index}: {title!r} -> ERROR: path does not exist: {root}")
            continue

        query_lower = query.lower()
        use_case_lower = use_case.lower()
        scored: list[tuple[int, str, int, str]] = []
        candidate_limit = max(limit * 20, 200)
        for file in _iter_files(root):
            if glob and not file.match(glob):
                continue
            if _is_binary(file):
                continue
            try:
                if file.stat().st_size > context.max_file_bytes:
                    continue
                text = file.read_text(errors="replace")
            except OSError:
                continue
            try:
                rel = str(file.relative_to(context.cwd))
            except ValueError:
                rel = str(file)
            rel_lower = rel.lower()
            best_for_file = 0
            for line_no, line in enumerate(text.splitlines(), 1):
                line_lower = line.lower()
                score = _line_score(line_lower, rel_lower, query_lower, use_case_lower, terms)
                if score == 0:
                    continue
                best_for_file = max(best_for_file, score)
                snippet = _truncate_line(line.strip(), min(context.max_line_chars, 240))
                scored.append((score, rel, line_no, snippet))
            if best_for_file:
                for term in terms:
                    if term in rel_lower:
                        scored.append((best_for_file + 12, rel, 1, f"[path match] {rel}"))
                        break
            if len(scored) > candidate_limit * 4:
                scored.sort(key=lambda row: (-row[0], row[1], row[2]))
                del scored[candidate_limit:]

        scored.sort(key=lambda row: (-row[0], row[1], row[2]))
        sections.append(f"\nQuery {query_index}: {title!r}")
        if use_case:
            sections.append(f"Use case: {use_case}")
        if raw_path or glob:
            scope = f"path={raw_path or '.'}"
            if glob:
                scope += f", glob={glob}"
            sections.append(f"Scope: {scope}")
        if not scored:
            sections.append("(no matches)")
            continue

        seen: set[tuple[str, int, str]] = set()
        emitted = 0
        for score, rel, line_no, snippet in scored:
            key = (rel, line_no, snippet)
            if key in seen:
                continue
            seen.add(key)
            line = f"{rel}:{line_no}: {snippet}"
            line_bytes = len(line.encode("utf-8")) + 1
            if output_bytes + line_bytes > budget:
                sections.append("[truncated: ToolContext max_tool_result_bytes reached]")
                return "\n".join(sections)
            sections.append(line)
            output_bytes += line_bytes
            emitted += 1
            if emitted >= limit:
                break

    return "\n".join(sections)


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
                "pattern": {"type": "string", "description": "Regular expression to match."},
                "path": {"type": "string", "description": "File or directory to search; defaults to the current working directory."},
                "glob": {"type": "string", "description": "Optional glob filter such as *.py or **/*.tsx."},
                "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"], "default": "files_with_matches", "description": "Result format: matching lines, paths only, or per-file counts."},
                "-B": {"type": "integer", "description": "Lines before each match when output_mode is content."},
                "-A": {"type": "integer", "description": "Lines after each match when output_mode is content."},
                "-C": {"type": "integer", "description": "Lines before and after each match when output_mode is content."},
                "-n": {"type": "boolean", "default": True, "description": "Include file:line prefixes for content output."},
                "-i": {"type": "boolean", "default": False, "description": "Match without case sensitivity."},
                "type": {"type": "string", "description": "File type or extension without dot, e.g. py or rs."},
                "head_limit": {"type": "integer", "description": "Maximum number of result entries after offset."},
                "offset": {"type": "integer", "description": "Number of result entries to skip before returning output."},
                "multiline": {"type": "boolean", "default": False, "description": "Allow the regex to span line breaks."},
            },
            required=("pattern",),
        ),
        Tool(
            "sem_search",
            load_description("sem_search"),
            sem_search,
            {"queries": {"type": "array", "items": {"type": "object"}, "description": "Natural-language query strings or objects with query/use_case/path/glob/limit."}},
            required=("queries",),
        ),
        Tool("remove", load_description("remove"), remove, {"path": {"type": "string", "description": "File or directory path to delete."}, "permanent": {"type": "boolean", "default": False, "description": "Delete directly after KING confirms permanent deletion."}}, required=("path",)),
        Tool(
            "patch",
            load_description("patch"),
            patch,
            {
                "file_path": {"type": "string", "description": "File path to edit."},
                "old_string": {"type": "string", "description": "Exact text to replace."},
                "new_string": {"type": "string", "description": "Replacement text; must differ from old_string."},
                "replace_all": {"type": "boolean", "default": False, "description": "Replace every occurrence instead of requiring one unique match."},
            },
            required=("file_path", "old_string", "new_string"),
        ),
        Tool(
            "multi_patch",
            load_description("multi_patch"),
            multi_patch,
            {
                "file_path": {"type": "string", "description": "File path to edit."},
                "edits": {"type": "array", "items": {"type": "object"}, "description": "Sequential exact replacements with old_string, new_string, and optional replace_all."},
            },
            required=("file_path", "edits"),
        ),
        Tool("undo", load_description("undo"), undo, {"path": {"type": "string", "description": "Path whose latest in-process snapshot should be restored."}}, required=("path",)),
    )
