"""What to tell the model about a path that does not exist: the nearest
existing directory, close names beside it, and existing paths that end the
same way."""

from __future__ import annotations

import difflib
import os
from collections import deque
from itertools import islice
from pathlib import Path

from .. import jail
from .core import ToolContext

_PATH_HINT_SKIP_DIRS = frozenset({".git", "node_modules", ".venv"})
_PATH_HINT_SCAN_MAX = 20_000
_PATH_HINT_SHOWN = 5


def _jail_shows(context: ToolContext, path: Path) -> bool:
    try:
        context.resolve_path(path)
    except jail.JailError:
        return False
    return True


def _same_tail_paths(context: ToolContext, target: Path) -> list[Path]:
    """Existing paths under the working directory and the jail root that end
    in the last one to three components of *target*, the longest shared tail
    first. The walk is breadth-first, skips .git, node_modules and .venv,
    and stops after _PATH_HINT_SCAN_MAX directory entries."""
    tail = target.parts[1:][-3:]
    if not tail:
        return []
    active = jail.active()
    roots = [Path(os.path.abspath(context.cwd))]
    if active is not None:
        roots.append(Path(os.path.abspath(active.root)))
    hidden = jail.hider(context)
    queue = deque(roots)
    visited: set[Path] = set()
    found: list[tuple[int, int, Path]] = []
    scanned = 0
    while queue and scanned < _PATH_HINT_SCAN_MAX:
        directory = queue.popleft()
        if directory in visited or (directory not in roots and hidden(directory)):
            continue
        visited.add(directory)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > _PATH_HINT_SCAN_MAX:
                        break
                    path = Path(entry.path)
                    if entry.name == tail[-1]:
                        shared = 1
                        while (shared < len(tail) and shared < len(path.parts) - 1
                               and path.parts[-shared - 1] == tail[-shared - 1]):
                            shared += 1
                        found.append((shared, len(found), path))
                    if entry.name not in _PATH_HINT_SKIP_DIRS and entry.is_dir(follow_symlinks=False):
                        queue.append(path)
        except OSError:
            continue
    found.sort(key=lambda item: (-item[0], item[1]))
    shown: list[Path] = []
    for _shared, _order, path in found:
        if path != target and not hidden(path) and _jail_shows(context, path):
            shown.append(path)
            if len(shown) == _PATH_HINT_SHOWN:
                break
    return shown


def missing_path_hint(context: ToolContext, target: Path) -> str:
    """Lines that point from a path that does not exist toward what does: the
    nearest existing ancestor directory, close names in the parent directory
    when it exists, and existing paths that end the same way (_same_tail_paths).
    Each line names only paths the jail shows."""
    lines: list[str] = []
    ancestor = next((parent for parent in target.parents
                     if parent.is_dir() and _jail_shows(context, parent)), None)
    if ancestor is not None:
        lines.append(f'Nearest existing directory: {ancestor}')
    if ancestor is not None and ancestor == target.parent:
        try:
            with os.scandir(ancestor) as entries:
                names = [entry.name for entry in islice(entries, _PATH_HINT_SCAN_MAX)]
        except OSError:
            names = []
        hidden = jail.hider(context)
        close = [name for name in difflib.get_close_matches(target.name, names, n=_PATH_HINT_SHOWN * 2, cutoff=0.6)
                 if not hidden(ancestor / name)][:_PATH_HINT_SHOWN]
        if close:
            lines.append('Close names there: {names}'.format(names=", ".join(close)))
    same_tail = _same_tail_paths(context, target)
    if same_tail:
        lines.append('Existing paths that end the same way:{paths}'.format(
            paths="".join(f"\n  {path}" for path in same_tail)))
    return "".join(f"\n{line}" for line in lines)


def no_such_file(context: ToolContext, target: Path) -> str:
    return f"ERROR: no such file: {target}{missing_path_hint(context, target)}"
