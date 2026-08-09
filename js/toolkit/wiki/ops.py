"""Ordered close-out for one ingested inbox unit."""
from __future__ import annotations

import shutil
from pathlib import Path

from ..core import ToolContext
from ..sanitize import text_or_default
from .helpers import resolve_vault, run, today, vault_lock


def _commit(vault: Path, message: str, context: ToolContext) -> str:
    if not (vault / ".git").is_dir():
        return "git: no repository"
    rc, _, err = run(["git", "-C", str(vault), "add", "-A"], context)
    if rc:
        return f"ERROR: git add failed: {err.strip()}"
    rc, _, _ = run(["git", "-C", str(vault), "diff", "--cached", "--quiet"], context)
    if rc == 0:
        return "git: nothing to commit"
    rc, out, err = run(["git", "-C", str(vault), "commit", "-m", message], context)
    if rc:
        return f"ERROR: git commit failed: {(err or out).strip()}"
    rc, sha, _ = run(["git", "-C", str(vault), "rev-parse", "--short", "HEAD"], context)
    return f"git: committed {sha.strip() if rc == 0 else '?'}"


def wiki_finish_ingest(
    vault: str,
    unit: str,
    title: str,
    note: str = "",
    context: ToolContext = None,
) -> str:
    """Archive one top-level inbox unit, append its log, then commit vault."""
    assert context is not None
    unit = text_or_default(unit).strip()
    title = text_or_default(title).strip()
    note = text_or_default(note).strip()
    if not unit or Path(unit).name != unit or unit in {".", "..", "_skipped"} or unit.startswith("."):
        return "ERROR: unit must be one visible top-level inbox name"
    if not title:
        return "ERROR: title is required"

    vp = resolve_vault(vault, context)
    if not (vp / "PURPOSE.md").is_file():
        return f"ERROR: no wiki vault at {vp}"
    src = vp / "inbox" / unit
    dest = vp / "Clippings" / unit
    if not src.exists():
        return f"ERROR: no inbox unit: {src}"
    if dest.exists():
        return f"ERROR: already archived: {dest}"

    with vault_lock(vp):
        (vp / "Clippings").mkdir(exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
            entry = f"\n## [{today()}] ingest | {title}\n{note}\n\nArchived: {unit} -> Clippings/{unit}\n"
            with (vp / "log.md").open("a", encoding="utf-8") as log:
                log.write(entry)
        except OSError as exc:
            if dest.exists() and not src.exists():
                shutil.move(str(dest), str(src))
            return f"ERROR: close-out failed and archive rolled back: {exc}"
        git = _commit(vp, f"ingest: {title}", context)

    return f"archived: inbox/{unit} -> Clippings/{unit}\nlogged: [{today()}] ingest | {title}\n{git}"
