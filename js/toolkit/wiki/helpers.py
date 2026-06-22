"""Shared helpers for the wiki toolkit."""
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
from ... import colors as C

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
def vault_lock(vault_path: Path, timeout_s: int = 30):
    """Exclusive cross-process advisory lock on a vault. Lets N parallel wiki
    writers (a fleet of ingest workers, the synth pass, drain's end-commit)
    serialize the moments that touch a SHARED file or the git index, so they
    never corrupt each other. flock is auto-released on context exit or if the
    process dies. Disjoint per-source writes don't contend; only same-file /
    same-log / git-commit moments actually block."""
    vault_path.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(vault_path / ".wiki.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def today() -> str:
    return date.today().isoformat()


def wiki_say(msg: str, context: ToolContext | None = None) -> None:
    """Concise human progress line for wiki runs.

    Prefer ToolContext.wiki_mode for in-process mode. Keep JS_WIKI_MODE as a
    compatibility/read-only fallback for direct tool tests and true subprocess
    boundaries.
    """
    import sys
    if (context is not None and getattr(context, "wiki_mode", "")) or os.environ.get("JS_WIKI_MODE"):
        print(f"  {C.BR_CYAN}[wiki]{C.RESET} {msg}", file=sys.stderr, flush=True)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")
    return s[:60] or "untitled"


def resolve_vault(vault: str, context: ToolContext) -> Path:
    raw = context.vault_aliases.get(str(vault).strip().lower(), str(vault))
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = context.cwd / p
    return p.resolve()


def find_vault(path: Path) -> Path | None:
    """Walk up to the vault root: a dir with a PURPOSE.md sentinel, or a wiki-* name.
    A bare `inbox/` is intentionally NOT a marker — it's too common a directory name
    and false-positives any home/project dir that happens to have one."""
    for parent in [path, *path.parents]:
        if (parent / "PURPOSE.md").exists() or parent.name.startswith("wiki-"):
            return parent
    return None


def collection_for(vault_path: Path) -> str:
    """qmd collection name == vault dir name (e.g. wiki-creative)."""
    return vault_path.name


def run(cmd: list[str], context: ToolContext, timeout: int = 300) -> tuple[int, str, str]:
    env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
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
    if not dest.exists():
        shutil.copy2(src, dest)
    return dest


def infer_vault(path: str | None, cwd: Path) -> str | None:
    """Best-effort vault id (absolute path) from a target path or cwd; None when
    nothing matches, so the caller can fail closed instead of guessing a vault."""
    for cand in (path, str(cwd)):
        if not cand:
            continue
        p = Path(os.path.expanduser(cand))
        if not p.is_absolute():
            p = cwd / p
        try:
            v = find_vault(p.resolve())
        except OSError:
            v = None
        if v:
            return str(v)
    return None
