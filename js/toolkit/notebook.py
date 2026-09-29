"""Jupyter notebooks: `read` shows an .ipynb as cells, `notebook_edit` changes
one cell by id.

A cell's id is its nbformat `id`; a notebook older than nbformat 4.5 has none,
and its cells are named `cell-N` by 0-based index. The notebook view records a
read of the file (path and content hash) but no line coverage: `patch` on the
raw JSON still needs a ranged `read` of the lines it edits.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from .core import Tool, ToolContext
from .descriptions import load_description
from .fs import _detect_line_ending, _hash_bytes, _normalize_line_endings, _read_regular_bytes
from .sanitize import int_or_default

EDIT_MODES = ("replace", "insert", "delete")
CELL_TYPES = ("code", "markdown", "raw")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_INDEXED_ID = re.compile(r"cell-(\d+)")
# Characters of one output's text shown per shown line.
_CHARS_PER_LINE = 200


class NotebookError(Exception):
    """The file is not a notebook js can edit. The message is one line."""


def _load(data: bytes) -> dict:
    try:
        notebook = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NotebookError(f"not valid notebook JSON: {exc}") from exc
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise NotebookError("not a notebook: no cells list")
    if not all(isinstance(cell, dict) for cell in notebook["cells"]):
        raise NotebookError("not a notebook: a cell is not an object")
    return notebook


def _text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return "" if value is None else str(value)


def cell_ids(cells: list[dict]) -> list[str]:
    """Each cell's id: its `id`, or `cell-N` when it has none."""
    return [cell["id"] if isinstance(cell.get("id"), str) and cell["id"] else f"cell-{index}"
            for index, cell in enumerate(cells)]


def find_cell(cells: list[dict], cell_id: str) -> int | None:
    """The index of the cell ``cell_id`` names: an `id`, else `cell-N`."""
    wanted = str(cell_id).strip()
    for index, cell in enumerate(cells):
        if cell.get("id") == wanted:
            return index
    match = _INDEXED_ID.fullmatch(wanted)
    if match and int(match.group(1)) < len(cells):
        return int(match.group(1))
    return None


def _language(notebook: dict) -> str:
    metadata = notebook.get("metadata") if isinstance(notebook.get("metadata"), dict) else {}
    info = metadata.get("language_info") if isinstance(metadata.get("language_info"), dict) else {}
    spec = metadata.get("kernelspec") if isinstance(metadata.get("kernelspec"), dict) else {}
    return str(info.get("name") or spec.get("language") or "")


def _clip(text: str, max_lines: int) -> list[str]:
    lines = _ANSI.sub("", text).rstrip("\n").splitlines()
    shown = [line if len(line) <= _CHARS_PER_LINE else line[:_CHARS_PER_LINE] + " [...]"
             for line in lines[:max_lines]]
    if len(lines) > max_lines:
        shown.append(f"[+{len(lines) - max_lines} more lines]")
    return shown


def _size(payload: Any) -> str:
    text = _text(payload)
    size = len(text) * 3 // 4 if text and not text.lstrip().startswith("<") else len(text.encode("utf-8"))
    return f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"


def summarize_output(output: dict, max_lines: int) -> list[str]:
    """One output as a heading line and at most ``max_lines`` indented lines of text."""
    kind = str(output.get("output_type", "output"))
    if kind == "stream":
        lines = _clip(_text(output.get("text")), max_lines)
        return [f"{output.get('name', 'stream')}:", *(f"  {line}" for line in lines)]
    if kind == "error":
        head = f"error {output.get('ename', '')}: {_ANSI.sub('', str(output.get('evalue', '')))}"
        return [head.rstrip(": ")]
    data = output.get("data") if isinstance(output.get("data"), dict) else {}
    others = [f"{mime} ({_size(payload)})" for mime, payload in data.items() if mime != "text/plain"]
    label = kind if kind != "execute_result" else "result"
    heading = f"{label}: " + ", ".join(["text/plain", *others] if "text/plain" in data else others)
    lines = _clip(_text(data.get("text/plain")), max_lines) if "text/plain" in data else []
    return [heading.rstrip(": ") + (":" if lines else ""), *(f"  {line}" for line in lines)]


def render(notebook: dict, path: Path, content_hash: str, max_lines: int) -> str:
    """The notebook as the `read` tool shows it."""
    cells = notebook["cells"]
    language = _language(notebook)
    version = f"nbformat {notebook.get('nbformat', '?')}.{notebook.get('nbformat_minor', '?')}"
    about = ", ".join(part for part in (language, version) if part)
    count = f"{len(cells)} cell" + ("" if len(cells) == 1 else "s")
    out = [f"{path}: notebook, {count} ({about}), hash {content_hash}"]
    for index, (cell, cell_id) in enumerate(zip(cells, cell_ids(cells))):
        kind = str(cell.get("cell_type", "code"))
        head = f"[cell {index} id={cell_id} {kind}"
        if kind == "code":
            count_value = cell.get("execution_count")
            head += f" [{count_value if count_value is not None else ' '}]"
        out.append("")
        out.append(head + "]")
        source = _text(cell.get("source"))
        out.append(source if source else "(empty)")
        outputs = cell.get("outputs") if isinstance(cell.get("outputs"), list) else []
        if outputs:
            out.append(f"[outputs of {cell_id}]")
            for output in outputs:
                if isinstance(output, dict):
                    out.extend(f"  {line}" for line in summarize_output(output, max_lines))
    return "\n".join(out)


def read_view(target: Path, context: ToolContext) -> str | None:
    """The `read` result for an .ipynb read without a range, or None when the
    file is not a notebook (it is then read as text)."""
    try:
        size = target.stat().st_size
        if size > context.max_file_bytes:
            return (f"ERROR: file size ({size} bytes) exceeds the maximum allowed size of "
                    f"{context.max_file_bytes} bytes")
        data = _read_regular_bytes(target)
    except OSError as exc:
        return f"ERROR: {exc}"
    try:
        notebook = _load(data)
    except NotebookError:
        return None
    content_hash = _hash_bytes(data)
    context.remember_read(target, content_hash, whole_file=False)
    context.remember_content(target, content_hash, data)
    max_lines = int_or_default(getattr(context, "notebook_output_lines", None), 10, minimum=0)
    return render(notebook, target, content_hash, max_lines)


def _indent(text: str) -> int:
    """The indent of the notebook's JSON, as Jupyter wrote it (1 by default)."""
    match = re.match(r"\s*\{[ \t]*\r?\n([ \t]+)\S", text)
    return len(match.group(1)) if match else 1


def _source_value(text: str, as_list: bool) -> str | list[str]:
    return text.splitlines(keepends=True) if as_list else text


def _new_cell(cell_type: str, source: str | list[str], cell_id: str | None) -> dict:
    cell: dict[str, Any] = {"cell_type": cell_type}
    if cell_type == "code":
        cell["execution_count"] = None
    if cell_id is not None:
        cell["id"] = cell_id
    cell["metadata"] = {}
    if cell_type == "code":
        cell["outputs"] = []
    cell["source"] = source
    return cell


def _retype(cell: dict, cell_type: str) -> None:
    cell["cell_type"] = cell_type
    if cell_type == "code":
        cell.pop("attachments", None)
        cell.setdefault("execution_count", None)
        cell.setdefault("outputs", [])
    else:
        cell.pop("execution_count", None)
        cell.pop("outputs", None)


def notebook_edit(
    file_path: str,
    cell_id: str | None = None,
    new_source: str | None = None,
    cell_type: str | None = None,
    edit_mode: str | None = "replace",
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    mode = str(edit_mode or "replace").strip().lower()
    if mode not in EDIT_MODES:
        return f"ERROR: edit_mode must be one of {', '.join(EDIT_MODES)}"
    kind = str(cell_type or "").strip().lower() or None
    if kind is not None and kind not in CELL_TYPES:
        return f"ERROR: cell_type must be one of {', '.join(CELL_TYPES)}"
    if mode == "insert" and kind is None:
        return "ERROR: cell_type is required to insert a cell"
    if mode != "delete" and new_source is None:
        return f"ERROR: new_source is required to {mode} a cell"
    if mode != "insert" and not cell_id:
        return f"ERROR: cell_id is required to {mode} a cell"
    if not file_path:
        return "ERROR: file_path is required"
    target = context.resolve_path(file_path, write=True)
    if target.suffix.lower() != ".ipynb":
        return f"ERROR: {target} is not an .ipynb notebook; edit other files with patch or write"
    if not target.is_file():
        return f"ERROR: no such notebook: {target}"
    try:
        data = _read_regular_bytes(target)
    except OSError as exc:
        return f"ERROR: {exc}"
    current_hash = _hash_bytes(data)
    guard = context.require_read(target, f"{mode} a cell of it", content_hash=current_hash)
    if guard:
        return guard
    try:
        notebook = _load(data)
    except NotebookError as exc:
        return f"ERROR: {target}: {exc}"
    cells: list[dict] = notebook["cells"]
    index = None
    if cell_id:
        index = find_cell(cells, cell_id)
        if index is None:
            ids = cell_ids(cells)
            listing = ", ".join(ids[:20]) + (f", ... ({len(ids)} cells)" if len(ids) > 20 else "")
            return f"ERROR: no cell {cell_id!r} in {target}; its cells are {listing or 'none'}"
    as_list = any(isinstance(cell.get("source"), list) for cell in cells) or not cells
    major = int_or_default(notebook.get("nbformat"), 4)
    minor = int_or_default(notebook.get("nbformat_minor"), 0)
    has_ids = major > 4 or (major == 4 and minor >= 5)

    if mode == "delete":
        assert index is not None
        removed_id = cell_ids(cells)[index]
        cells.pop(index)
        done = f"deleted cell {removed_id}"
    elif mode == "insert":
        assert kind is not None and new_source is not None
        taken = set(cell_ids(cells))
        new_id = None
        if has_ids:
            new_id = uuid.uuid4().hex[:8]
            while new_id in taken:
                new_id = uuid.uuid4().hex[:8]
        index = 0 if index is None else index + 1
        cells.insert(index, _new_cell(kind, _source_value(new_source, as_list), new_id))
        done = f"inserted {kind} cell {new_id or f'cell-{index}'} at index {index}"
    else:
        assert index is not None and new_source is not None
        cell = cells[index]
        cell["source"] = _source_value(new_source, isinstance(cell.get("source"), list) or as_list)
        if kind is not None and kind != cell.get("cell_type"):
            _retype(cell, kind)
        if cell.get("cell_type") == "code":
            cell["execution_count"] = None
            cell["outputs"] = []
        done = f"replaced cell {cell_ids(cells)[index]} ({cell.get('cell_type', 'code')}); its outputs are cleared"

    original = data.decode("utf-8")
    text = json.dumps(notebook, indent=_indent(original), ensure_ascii=False)
    if original.endswith(("\n", "\r")):
        text += "\n"
    payload = _normalize_line_endings(text, _detect_line_ending(original)).encode("utf-8")
    try:
        context.snapshot(target)
        target.write_bytes(payload)
    except OSError as exc:
        return f"ERROR: {exc}"
    new_hash = _hash_bytes(payload)
    # The model knows the notebook it just changed; the raw JSON lines moved,
    # so no line coverage carries over.
    context.replace_read_coverage(target, new_hash, [], len(payload.splitlines()), whole_file=False)
    context.remember_content(target, new_hash, payload)
    return f"{done} in {target}; {len(cells)} cells now (hash {new_hash})"


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "notebook_edit",
            load_description("notebook_edit"),
            notebook_edit,
            {
                "file_path": {"type": "string", "description": "Path to the .ipynb notebook."},
                "cell_id": {"type": "string", "description": "Id of the cell to replace or delete, or to insert after. Omit to insert at the top."},
                "new_source": {"type": "string", "description": "The cell's whole new source. Not used by delete."},
                "cell_type": {"type": "string", "enum": list(CELL_TYPES), "description": "Required to insert; on replace, changes the cell's type."},
                "edit_mode": {"type": "string", "enum": list(EDIT_MODES), "default": "replace", "description": "replace, insert or delete."},
            },
            required=("file_path",),
        ),
    )
