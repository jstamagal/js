"""Core tool contracts for the js agent.

The runtime depends on this module, not on individual tool implementations.
Tools are small Python objects with an OpenAI-compatible schema and a handler
that receives a shared ToolContext. The context carries per-session state used
for read-before-write checks, undo snapshots, todos, and search deduplication.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable

from ..paths import state_root


Handler = Callable[..., Any]
Snapshot = bytes | None | dict[str, Any]

_SNAPSHOT_FORMAT_VERSION = 1
_SNAPSHOT_MAX_ENTRIES = 100
_SNAPSHOT_MAX_DISK_BYTES = 64 * 1024 * 1024


def _merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _encode_snapshot(path: Path, snapshot: Snapshot) -> bytes:
    if snapshot is None:
        payload: dict[str, Any] = {"kind": "missing"}
    elif isinstance(snapshot, bytes):
        payload = {"kind": "file", "data": base64.b64encode(snapshot).decode("ascii")}
    elif snapshot.get("kind") == "symlink":
        payload = {"kind": "symlink", "target": str(snapshot.get("target", ""))}
    elif snapshot.get("kind") == "directory":
        entries = []
        for rel, data in snapshot.get("entries", {}).items():
            encoded = None if data is None else base64.b64encode(data).decode("ascii")
            entries.append([rel, encoded])
        payload = {"kind": "directory", "entries": entries}
    else:
        raise ValueError("unsupported snapshot kind")
    envelope = {
        "version": _SNAPSHOT_FORMAT_VERSION,
        "path": str(path),
        "snapshot": payload,
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decode_snapshot(data: bytes, expected_path: Path) -> Snapshot:
    envelope = json.loads(data.decode("utf-8"))
    if not isinstance(envelope, dict) or envelope.get("version") != _SNAPSHOT_FORMAT_VERSION:
        raise ValueError("unsupported snapshot format")
    if envelope.get("path") != str(expected_path):
        raise ValueError("snapshot path does not match its store")
    payload = envelope.get("snapshot")
    if not isinstance(payload, dict):
        raise ValueError("snapshot payload is not an object")
    kind = payload.get("kind")
    if kind == "missing":
        return None
    if kind == "file":
        encoded = payload.get("data")
        if not isinstance(encoded, str):
            raise ValueError("file snapshot has no data")
        return base64.b64decode(encoded, validate=True)
    if kind == "symlink":
        target = payload.get("target")
        if not isinstance(target, str):
            raise ValueError("symlink snapshot has no target")
        return {"kind": "symlink", "target": target}
    if kind == "directory":
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise ValueError("directory snapshot has no entries")
        entries: dict[str, bytes | None] = {}
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, list) or len(raw_entry) != 2:
                raise ValueError("invalid directory snapshot entry")
            rel, encoded = raw_entry
            rel_path = Path(rel) if isinstance(rel, str) else Path("..")
            if not isinstance(rel, str) or rel_path.is_absolute() or ".." in rel_path.parts:
                raise ValueError("unsafe directory snapshot path")
            if encoded is not None and not isinstance(encoded, str):
                raise ValueError("invalid directory snapshot data")
            entries[rel] = None if encoded is None else base64.b64decode(encoded, validate=True)
        return {"kind": "directory", "entries": entries}
    if kind == "unavailable":
        reason = payload.get("reason")
        return {"kind": "unavailable", "reason": str(reason or "snapshot was not persisted")}
    raise ValueError(f"unsupported snapshot kind {kind!r}")


def _snapshot_raw_size(snapshot: Snapshot) -> int:
    if snapshot is None:
        return 0
    if isinstance(snapshot, bytes):
        return len(snapshot)
    if snapshot.get("kind") == "directory":
        return sum(
            len(str(rel).encode("utf-8")) + (len(data) if isinstance(data, bytes) else 0)
            for rel, data in snapshot.get("entries", {}).items()
        )
    return len(str(snapshot.get("target", "")).encode("utf-8"))


@dataclass(frozen=True)
class ToolResult:
    """A provider-facing mixed tool result plus a safe persistence descriptor."""

    blocks: list[dict[str, Any]]
    is_error: bool = False

    @classmethod
    def text(cls, text: str, *, is_error: bool = False) -> ToolResult:
        return cls([{"type": "text", "text": text}], is_error=is_error)

    def dehydrated(self) -> str:
        lines: list[str] = []
        for block in self.blocks:
            kind = str(block.get("type", "unknown"))
            if kind == "text":
                lines.append(str(block.get("text", "")))
            elif kind in {"image", "audio"}:
                lines.append(f"[{kind} {block.get('mimeType', 'application/octet-stream')} omitted from history]")
            elif kind == "resource_link":
                lines.append(f"[resource link {block.get('name', '')}: {block.get('uri', '')}]")
            elif kind == "resource":
                resource = block.get("resource")
                if isinstance(resource, dict):
                    uri = resource.get("uri", "")
                    text = resource.get("text")
                    lines.append(f"[embedded resource {uri}]" + (f"\n{text}" if isinstance(text, str) else " [binary omitted]"))
            elif kind == "structured":
                lines.append(compact_json(block.get("value")))
            else:
                lines.append(f"[{kind} content omitted from history]")
        value = "\n".join(lines)
        return f"ERROR: {value}" if self.is_error and not value.startswith("ERROR") else value


@dataclass(frozen=True)
class CatalogEntry:
    """Compact metadata for one discoverable turn-scoped capability."""

    id: str
    name: str
    description: str
    kind: str
    source: str
    loadable: bool = True


@dataclass(frozen=True)
class Tool:
    """OpenAI-compatible tool declaration plus Python handler."""

    name: str
    description: str
    handler: Handler
    params: dict[str, dict]
    required: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    input_schema: dict[str, Any] | None = None

    def openai_spec(self) -> dict:
        parameters = self.input_schema if self.input_schema is not None else {
            "type": "object",
            "properties": self.params,
            "required": list(self.required),
            "additionalProperties": False,
        }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


@dataclass
class Todo:
    content: str
    status: str


@dataclass
class ToolContext:
    """Mutable state shared across tool calls in a js process."""

    cwd: Path = field(default_factory=Path.cwd)
    max_read_lines: int = 2_000
    max_line_chars: int = 2_000
    jsonl_max_line_chars: int = 65536
    max_file_bytes: int = 2_000_000
    max_read_bytes: int = 256 * 1024
    max_tool_result_bytes: int = 256 * 1024
    max_bash_output_bytes: int = 256 * 1024
    max_bash_output_ceiling: int = 150_000
    max_tool_result_inline_bytes: int = 51_200
    fetch_timeout_s: int = 15
    browse_timeout_s: int = 60
    download_timeout_s: int = 300
    max_download_bytes: int = 0           # 0 = unlimited; save= streams to disk
    task_max_depth: int = 2
    subagent_max_workers: int = 8
    vision_enabled: bool = False
    model: str = ""                       # model id, for toolbox revision provenance
    kernel_verbosity: str = "normal"      # quiet | normal | verbose terminal render
    kernel_render_max_lines: int = 24     # per-section line cap on that render
    kernel_session: Any = None            # the live IPython kernel, one per process
    read_paths: set[Path] = field(default_factory=set)
    file_hashes: dict[Path, str] = field(default_factory=dict)
    read_ranges: dict[Path, list[tuple[int, int]]] = field(default_factory=dict)
    read_line_totals: dict[Path, int] = field(default_factory=dict)
    fully_read_paths: set[Path] = field(default_factory=set)
    snapshots: dict[Path, list[Snapshot]] = field(default_factory=dict)
    snapshot_files: dict[Path, list[Path | None]] = field(default_factory=dict, repr=False)
    snapshot_store: Path | None = field(default=None, repr=False)
    search_cache: dict[str, str] = field(default_factory=dict)
    todos: dict[str, Todo] = field(default_factory=dict)
    terminal_sessions: dict[str, Any] = field(default_factory=dict)
    last_prompt_tokens: int = 0
    last_cached_tokens: int = 0
    last_output_tokens: int = 0
    last_max_output_tokens: int | None = None
    last_incomplete_reason: str | None = None
    _snapshot_lock: Any = field(default_factory=threading.RLock, init=False, repr=False)
    _snapshot_notices: dict[int, list[str]] = field(default_factory=dict, init=False, repr=False)

    def resolve_path(self, raw: str | os.PathLike[str]) -> Path:
        path = Path(os.path.expanduser(str(raw)))
        if not path.is_absolute():
            path = self.cwd / path
        return path.resolve()

    def remember_read(
        self,
        path: Path,
        content_hash: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        total_lines: int | None = None,
        whole_file: bool | None = None,
    ) -> None:
        """Record the content the model actually saw, not merely its path.

        Callers that omit a range retain the historical meaning of a complete
        read. Text readers pass their rendered window explicitly, including when
        an implicit max-lines page made a nominal whole-file read partial.
        """
        previous_hash = self.file_hashes.get(path)
        if previous_hash is not None and previous_hash != content_hash:
            self.read_ranges.pop(path, None)
            self.read_line_totals.pop(path, None)
            self.fully_read_paths.discard(path)
        self.read_paths.add(path)
        self.file_hashes[path] = content_hash
        if total_lines is not None:
            self.read_line_totals[path] = max(0, total_lines)
        if whole_file is None:
            whole_file = start_line is None and end_line is None
        if whole_file:
            self.fully_read_paths.add(path)
            if total_lines:
                self.read_ranges[path] = [(1, total_lines)]
            return
        if start_line is None or end_line is None or end_line < start_line:
            return
        ranges = [*self.read_ranges.get(path, []), (start_line, end_line)]
        self.read_ranges[path] = _merge_line_ranges(ranges)
        total = self.read_line_totals.get(path)
        if total == 0 or (total is not None and self._ranges_cover(path, 1, total)):
            self.fully_read_paths.add(path)

    def _ranges_cover(
        self,
        path: Path,
        start: int,
        end: int,
        seen_ranges: list[tuple[int, int]] | None = None,
    ) -> bool:
        if seen_ranges is None and path in self.fully_read_paths:
            return True
        cursor = start
        for seen_start, seen_end in seen_ranges if seen_ranges is not None else self.read_ranges.get(path, []):
            if seen_end < cursor:
                continue
            if seen_start > cursor:
                return False
            cursor = max(cursor, seen_end + 1)
            if cursor > end:
                return True
        return cursor > end

    def require_read(
        self,
        path: Path,
        action: str,
        *,
        line_ranges: list[tuple[int, int]] | None = None,
        whole_file: bool = False,
        content_hash: str | None = None,
        seen_ranges: list[tuple[int, int]] | None = None,
    ) -> str | None:
        if path not in self.read_paths:
            return f"ERROR: You must read the file with the read tool before attempting to {action}."
        if content_hash is not None and self.file_hashes.get(path) != content_hash:
            return f"ERROR: {path} changed since it was read; read it again before attempting to {action}."
        if whole_file and path not in self.fully_read_paths:
            return f"ERROR: You must read the whole file before attempting to {action}."
        unseen = [span for span in line_ranges or [] if not self._ranges_cover(path, *span, seen_ranges)]
        if unseen:
            rendered = ", ".join(str(start) if start == end else f"{start}-{end}" for start, end in unseen)
            return (
                f"ERROR: You must read the target line{'s' if len(unseen) != 1 or unseen[0][0] != unseen[0][1] else ''} "
                f"({rendered}) before attempting to {action}."
            )
        return None

    def replace_read_coverage(
        self,
        path: Path,
        content_hash: str,
        ranges: list[tuple[int, int]],
        total_lines: int,
        *,
        whole_file: bool,
    ) -> None:
        self.read_paths.add(path)
        self.file_hashes[path] = content_hash
        self.read_ranges[path] = _merge_line_ranges(ranges)
        self.read_line_totals[path] = max(0, total_lines)
        if whole_file:
            self.fully_read_paths.add(path)
        else:
            self.fully_read_paths.discard(path)

    def configure_snapshot_store(
        self,
        agent_id: str,
        session_file: Path,
        *,
        state_dir: Path | None = None,
    ) -> None:
        """Attach this context to one session's bounded persistent undo store."""
        session = Path(session_file).expanduser().resolve(strict=False)
        if session == Path(os.devnull).resolve(strict=False):
            store = None
        else:
            safe_agent = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in agent_id) or "defaultagent"
            safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in session.stem) or "session"
            session_hash = hashlib.sha256(str(session).encode("utf-8")).hexdigest()[:16]
            store = (state_dir or state_root()) / safe_agent / "undo" / f"{safe_stem}-{session_hash}"
        with self._snapshot_lock:
            if store is None:
                self.snapshots.clear()
                self.snapshot_files.clear()
                self.snapshot_store = None
                return
            if store == self.snapshot_store:
                return
            self.snapshots.clear()
            self.snapshot_files.clear()
            self.snapshot_store = store
            if store is not None:
                self._load_snapshots()

    def _path_snapshot_dir(self, path: Path) -> Path:
        assert self.snapshot_store is not None
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        return self.snapshot_store / digest

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)

    def _notice_snapshot(self, message: str) -> None:
        ident = threading.get_ident()
        self._snapshot_notices.setdefault(ident, []).append(message)

    def consume_snapshot_notices(self) -> list[str]:
        with self._snapshot_lock:
            return self._snapshot_notices.pop(threading.get_ident(), [])

    def _load_snapshots(self) -> None:
        assert self.snapshot_store is not None
        try:
            for temporary in self.snapshot_store.glob("*/.*.tmp"):
                try:
                    temporary.unlink()
                except OSError:
                    pass
            path_dirs = sorted(path for path in self.snapshot_store.iterdir() if path.is_dir())
        except FileNotFoundError:
            return
        except OSError:
            return
        for path_dir in path_dirs:
            try:
                metadata = json.loads((path_dir / "path.json").read_text(encoding="utf-8"))
                target = Path(metadata["path"])
                if not target.is_absolute():
                    raise ValueError("snapshot target is not absolute")
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            try:
                entries = sorted(path_dir.glob("*.snapshot"))
            except OSError:
                continue
            for entry in entries:
                try:
                    snapshot = _decode_snapshot(entry.read_bytes(), target)
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    snapshot = {"kind": "corrupt", "reason": f"{type(exc).__name__}: {exc}"}
                self.snapshots.setdefault(target, []).append(snapshot)
                self.snapshot_files.setdefault(target, []).append(entry)
        self._prune_snapshot_store()

    def _persist_snapshot(self, path: Path, snapshot: Snapshot) -> Path | None:
        if self.snapshot_store is None:
            return None
        path_dir = self._path_snapshot_dir(path)
        try:
            metadata = json.dumps(
                {"version": _SNAPSHOT_FORMAT_VERSION, "path": str(path)},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self._atomic_write(path_dir / "path.json", metadata)
            too_large = _snapshot_raw_size(snapshot) > _SNAPSHOT_MAX_DISK_BYTES
            encoded = b"" if too_large else _encode_snapshot(path, snapshot)
            if too_large or len(encoded) > _SNAPSHOT_MAX_DISK_BYTES:
                reason = f"snapshot exceeds the {_SNAPSHOT_MAX_DISK_BYTES // (1024 * 1024)} MiB session undo cap"
                encoded = json.dumps(
                    {
                        "version": _SNAPSHOT_FORMAT_VERSION,
                        "path": str(path),
                        "snapshot": {"kind": "unavailable", "reason": reason},
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                self._notice_snapshot(f"{reason}; this undo is available only until the process exits")
            stamp = f"{time.time_ns():020d}-{secrets.token_hex(6)}.snapshot"
            entry = path_dir / stamp
            self._atomic_write(entry, encoded)
            self._prune_snapshot_store()
            return entry if entry.exists() else None
        except OSError as exc:
            self._notice_snapshot(f"could not persist undo snapshot for {path}: {exc}")
            return None

    def _forget_snapshot_file(self, victim: Path) -> None:
        for files in self.snapshot_files.values():
            for index, entry in enumerate(files):
                if entry == victim:
                    files[index] = None

    @staticmethod
    def _cleanup_snapshot_dir(path_dir: Path) -> None:
        try:
            if not any(path_dir.glob("*.snapshot")):
                (path_dir / "path.json").unlink(missing_ok=True)
                path_dir.rmdir()
        except OSError:
            pass

    def _prune_snapshot_store(self) -> None:
        assert self.snapshot_store is not None
        try:
            entries = list(self.snapshot_store.glob("*/*.snapshot"))
            sized = [(entry, entry.stat().st_size) for entry in entries]
        except OSError:
            return
        sized.sort(key=lambda item: item[0].name)
        total = sum(size for _entry, size in sized)
        while sized and (len(sized) > _SNAPSHOT_MAX_ENTRIES or total > _SNAPSHOT_MAX_DISK_BYTES):
            victim, size = sized.pop(0)
            try:
                victim.unlink()
            except OSError:
                break
            total -= size
            self._forget_snapshot_file(victim)
            self._cleanup_snapshot_dir(victim.parent)

    def record_snapshot(self, path: Path, snapshot: Snapshot) -> None:
        with self._snapshot_lock:
            self.snapshots.setdefault(path, []).append(snapshot)
            entry = self._persist_snapshot(path, snapshot)
            self.snapshot_files.setdefault(path, []).append(entry)

    def pop_snapshot(self, path: Path) -> tuple[bool, Snapshot]:
        with self._snapshot_lock:
            stack = self.snapshots.get(path) or []
            if not stack:
                return False, None
            snapshot = stack.pop()
            files = self.snapshot_files.get(path) or []
            entry = files.pop() if files else None
            if entry is not None:
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass
                self._cleanup_snapshot_dir(entry.parent)
            return True, snapshot

    def discard_snapshot(self, path: Path) -> None:
        self.pop_snapshot(path)

    def invalidate_search_cache(self) -> None:
        """Drop memoized fs_search results after anything may have changed the tree.

        The dedup cache is keyed only on the search arguments, so without this a
        model that edits a file and re-runs the same search gets the PRE-EDIT hit
        list back, labelled `[deduplicated repeated search]` — it looks like a
        confirmation that nothing changed. Every mutating fs tool funnels through
        `snapshot()`; `shell` clears it directly because a command can touch
        anything."""
        self.search_cache.clear()

    def snapshot(self, path: Path) -> None:
        self.invalidate_search_cache()
        try:
            if path.is_dir():
                entries: dict[str, bytes | None] = {}
                for child in sorted(path.rglob("*")):
                    rel = child.relative_to(path).as_posix()
                    if child.is_dir():
                        entries[rel + "/"] = None
                    elif child.is_file():
                        entries[rel] = child.read_bytes()
                content: Snapshot = {"kind": "directory", "entries": entries}
            else:
                content = path.read_bytes() if path.exists() else None
        except OSError:
            content = None
        self.record_snapshot(path, content)


def coerce_value(value: Any, schema_type: str | None) -> Any:
    if schema_type is None or value is None:
        return value
    try:
        if schema_type == "integer" and not isinstance(value, bool) and not isinstance(value, int):
            return int(value)
        if schema_type == "number" and not isinstance(value, (int, float, bool)):
            return float(value)
        if schema_type == "string" and not isinstance(value, str):
            return str(value)
        if schema_type == "boolean" and not isinstance(value, bool):
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"1", "true", "yes", "y", "on"}:
                    return True
                if lowered in {"0", "false", "no", "n", "off"}:
                    return False
    except (TypeError, ValueError):
        pass
    return value


def call_tool(tool: Tool, args: dict[str, Any], context: ToolContext) -> Any:
    """Filter/coerce model args and invoke a tool handler."""

    sig = inspect.signature(tool.handler)
    known = set(sig.parameters)
    has_var_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values())
    # "context" is the ToolContext injection slot only when the tool does not
    # declare it in its model-facing schema (remote MCP schemas may require a
    # real property with that name; its value must reach the server).
    declares_context = "context" in tool.params
    filtered: dict[str, Any] = {}
    for key, value in args.items():
        if key not in known and not has_var_kwargs:
            continue
        if key == "context" and not declares_context:
            continue
        schema_type = tool.params.get(key, {}).get("type")
        filtered[key] = coerce_value(value, schema_type)
    if "context" in known and not declares_context:
        filtered["context"] = context
    result = tool.handler(**filtered)
    notices = context.consume_snapshot_notices()
    if notices and isinstance(result, str):
        rendered = "\n".join(f"WARNING: {notice}" for notice in notices)
        return f"{result}\n{rendered}"
    return result


async def call_tool_async(tool: Tool, args: dict[str, Any], context: ToolContext) -> Any:
    """Invoke either a native sync handler or a cancelable async handler."""
    result = call_tool(tool, args, context)
    return await result if inspect.isawaitable(result) else result


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
