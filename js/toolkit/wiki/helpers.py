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
from tempfile import NamedTemporaryFile

from ... import jail
from ...capped_process import truncation_marker
from ...text_bytes import cap_text
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
def vault_lock(vault_path: Path, context: ToolContext):
    vault_path.mkdir(parents=True, exist_ok=True)
    lock_path = context.resolve_path(vault_path / ".wiki.lock", write=True)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
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
    return context.resolve_path(vault, write=True)


def find_vault(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if (parent / "PURPOSE.md").exists():
            return parent
    return None


def run(cmd: list[str], context: ToolContext, timeout: int = 300) -> tuple[int, str, str]:
    """Run ``cmd`` with the allowlisted environment, in the jail under `js -C`."""
    env = {key: os.environ[key] for key in _ENV_ALLOW if key in os.environ}
    argv = jail.wrap(cmd, context, cwd=context.cwd, env=env)
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 1, "", str(exc)
    cap = context.max_tool_result_bytes
    marker = truncation_marker(cap, "TOOL_RESULT_MAX")
    return (
        proc.returncode,
        cap_text(proc.stdout.decode("utf-8", "replace"), cap, marker),
        cap_text(proc.stderr.decode("utf-8", "replace"), cap, marker),
    )


def read_text(path: Path, cap: int) -> str:
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError as exc:
        return f"ERROR: {exc}"
    return cap_text(text, cap, truncation_marker(cap, "TOOL_RESULT_MAX"))


def _existing_asset(src: Path, dest: Path) -> Path | str:
    try:
        identical = dest.read_bytes() == src.read_bytes()
    except OSError as exc:
        return f"ERROR: could not compare {src} with {dest}: {exc}"
    if not identical:
        return (
            f"ERROR: asset name collision at {dest}: a different file already "
            f"uses that name; rename {src.name} and convert again"
        )
    return dest


def copy_to_assets(src: Path, vault_path: Path, context: ToolContext) -> Path | str:
    """Copy a media file into <vault>/assets and return the destination path.

    A missing vault or an existing same-named file with different bytes comes
    back as an "ERROR: ..." string instead of an exception escaping the tool.
    """
    if not vault_path.is_dir():
        return f"ERROR: no vault at {vault_path}"
    assets = context.resolve_path(vault_path / "assets", write=True)
    try:
        assets.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"ERROR: could not create asset directory {assets}: {exc}"
    dest = context.resolve_path(assets / src.name, write=True)
    if dest.exists():
        return _existing_asset(src, dest)
    try:
        with vault_lock(vault_path, context), NamedTemporaryFile(dir=assets, prefix=".wiki-asset-") as staged:
            shutil.copy2(src, staged.name)
            try:
                os.link(staged.name, dest)
            except FileExistsError:
                return _existing_asset(src, dest)
    except OSError as exc:
        return f"ERROR: could not copy {src} to {dest}: {exc}"
    return dest
