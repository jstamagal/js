"""Core tool contracts for the js agent.

The runtime depends on this module, not on individual tool implementations.
Tools are small Python objects with an OpenAI-compatible schema and a handler
that receives a shared ToolContext. The context carries per-session state used
for read-before-write checks, undo snapshots, todos, and search deduplication.
"""

from __future__ import annotations

import inspect
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable


Handler = Callable[..., str]
Snapshot = bytes | None | dict[str, Any]


@dataclass(frozen=True)
class Tool:
    """OpenAI-compatible tool declaration plus Python handler."""

    name: str
    description: str
    handler: Handler
    params: dict[str, dict]
    required: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def openai_spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.params,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
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
    max_tool_result_bytes: int = 256 * 1024
    max_bash_output_bytes: int = 256 * 1024
    fetch_timeout_s: int = 15
    task_max_depth: int = 2
    subagent_max_workers: int = 8
    wiki_vault_lock_timeout_s: int = 30
    wiki_mode: str = ""
    wiki_no_archive: bool = False
    vault_aliases: dict[str, str] = field(default_factory=dict)  # wiki vault alias -> path, from config
    artifact_dir: Path | None = None
    artifact_url: str | None = None
    artifact_bin: str | None = None
    vision_enabled: bool = False
    read_paths: set[Path] = field(default_factory=set)
    file_hashes: dict[Path, str] = field(default_factory=dict)
    snapshots: dict[Path, list[Snapshot]] = field(default_factory=dict)
    search_cache: dict[str, str] = field(default_factory=dict)
    todos: dict[str, Todo] = field(default_factory=dict)

    def resolve_path(self, raw: str | os.PathLike[str]) -> Path:
        path = Path(os.path.expanduser(str(raw)))
        if not path.is_absolute():
            path = self.cwd / path
        return path.resolve()

    def remember_read(self, path: Path, content_hash: str) -> None:
        self.read_paths.add(path)
        self.file_hashes[path] = content_hash

    def require_read(self, path: Path, action: str) -> str | None:
        if path in self.read_paths:
            return None
        return f"ERROR: You must read the file with the read tool before attempting to {action}."

    def snapshot(self, path: Path) -> None:
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
        self.snapshots.setdefault(path, []).append(content)


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


def call_tool(tool: Tool, args: dict[str, Any], context: ToolContext) -> str:
    """Filter/coerce model args and invoke a tool handler."""

    sig = inspect.signature(tool.handler)
    known = set(sig.parameters)
    has_var_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values())
    filtered: dict[str, Any] = {}
    for key, value in args.items():
        if key not in known and not has_var_kwargs:
            continue
        if key == "context":
            continue
        schema_type = tool.params.get(key, {}).get("type")
        filtered[key] = coerce_value(value, schema_type)
    if "context" in known:
        filtered["context"] = context
    return tool.handler(**filtered)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
