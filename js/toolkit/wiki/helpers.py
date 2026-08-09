"""Shared helpers for wiki tools."""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from ..core import ToolContext

KIND_FOLDER = {
    "source": "sources", "source-summary": "sources",
    "entity": "entities", "concept": "concepts", "synthesis": "synthesis",
}
KIND_TYPE = {
    "source": "source-summary", "source-summary": "source-summary",
    "entity": "entity", "concept": "concept", "synthesis": "synthesis",
}
KIND_TAG = {
    "source-summary": "wiki/source", "entity": "wiki/entity",
    "concept": "wiki/concept", "synthesis": "wiki/synthesis",
}
_ENV_ALLOW = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "PWD", "SHELL")


@contextmanager
def vault_lock(vault_path: Path):
    vault_path.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(vault_path / ".wiki.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def today() -> str:
    return date.today().isoformat()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")[:60] or "untitled"


def resolve_vault(vault: str, context: ToolContext) -> Path:
    path = Path(os.path.expanduser(str(vault)))
    return (path if path.is_absolute() else context.cwd / path).resolve()


def find_vault(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if (parent / "PURPOSE.md").exists():
            return parent
    return None


def run(cmd: list[str], context: ToolContext, timeout: int = 300) -> tuple[int, str, str]:
    env = {key: os.environ[key] for key in _ENV_ALLOW if key in os.environ}
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 1, "", str(exc)
    cap = context.max_tool_result_bytes
    return (
        proc.returncode,
        proc.stdout[:cap].decode("utf-8", "replace"),
        proc.stderr[:cap].decode("utf-8", "replace"),
    )


def read_text(path: Path, cap: int) -> str:
    try:
        return path.read_text("utf-8", errors="replace")[:cap]
    except OSError as exc:
        return f"ERROR: {exc}"


def copy_to_assets(src: Path, vault_path: Path) -> Path:
    assets = vault_path / "assets"
    assets.mkdir(exist_ok=True)
    dest = assets / src.name
    if dest.exists() and dest.read_bytes() != src.read_bytes():
        raise FileExistsError(f"asset name collision: {dest}")
    if not dest.exists():
        shutil.copy2(src, dest)
    return dest
