"""wiki ops: purpose, inbox, search, archive, log."""
from __future__ import annotations

import os
import re
import shutil

from ..core import ToolContext
from ..sanitize import text_or_default
from ... import colors as C
from .helpers import resolve_vault, collection_for, run, read_text, today, vault_lock, wiki_say

_SECTIONS = ("sources", "entities", "concepts", "synthesis")
_SOURCE_FIELD_RE = re.compile(r'^\s*source:\s*"?([^"\n]+?)"?\s*$', re.MULTILINE)


def _find_orphans(vault_path) -> list[tuple[str, str]]:
    """Return [(inbox_unit, source_page_name), ...] for inbox files that already
    have a source page pointing at them. Means a prior ingest wrote the page
    but failed to archive — the local LLM dropped the wiki_archive call.
    Surfacing this lets the next run self-heal."""
    inbox = vault_path / "inbox"
    sources_dir = vault_path / "sources"
    if not inbox.is_dir() or not sources_dir.is_dir():
        return []
    inbox_names = {e.name for e in inbox.iterdir()
                   if e.name != "_skipped" and not e.name.startswith(".")}
    if not inbox_names:
        return []
    orphans: list[tuple[str, str]] = []
    for page in sources_dir.glob("*.md"):
        try:
            head = page.read_text("utf-8", errors="replace")[:2048]
        except OSError:
            continue
        m = _SOURCE_FIELD_RE.search(head)
        if not m:
            continue
        src_val = m.group(1).strip()
        # source field can be "inbox/X", "Clippings/X", or bare "X"
        candidate = src_val.split("/", 1)[1] if "/" in src_val else src_val
        if candidate in inbox_names:
            orphans.append((candidate, page.name))
    return orphans


def wiki_purpose(vault: str, context: ToolContext = None) -> str:
    assert context is not None
    vp = resolve_vault(vault, context)
    if not vp.is_dir():
        return f"ERROR: no vault at {vp}"
    out = [f"# Vault: {vp}", ""]
    purpose = vp / "PURPOSE.md"
    out.append(read_text(purpose, context.max_tool_result_bytes) if purpose.exists()
               else "(no PURPOSE.md yet — ask KING what this wiki is for before ingesting)")
    out.append("\n## Counts")
    for sec in _SECTIONS:
        d = vp / sec
        n = len(list(d.glob("*.md"))) if d.is_dir() else 0
        out.append(f"- {sec}: {n}")
    inbox = vp / "inbox"
    if inbox.is_dir():
        units = [e.name for e in sorted(inbox.iterdir()) if e.name != "_skipped" and not e.name.startswith(".")]
        out.append(f"- inbox units waiting: {len(units)}")
    # Leave-in-place mode never archives, so every successful ingest would
    # otherwise be flagged ORPHAN forever — skip the scan entirely there.
    leave_in_place = getattr(context, "wiki_no_archive", False) or os.environ.get("JS_WIKI_NO_ARCHIVE")
    orphans = [] if leave_in_place else _find_orphans(vp)
    if orphans:
        out.append("\n## ⚠ ORPHANS (source page exists but unit not archived — prior run dropped wiki_archive)")
        for unit, page in orphans:
            out.append(f"- inbox/{unit}  ← documented by sources/{page}  → call wiki_archive(vault, \"{unit}\")")
    wiki_say(f"purpose loaded: {vp.name}", context)
    return "\n".join(out)


def wiki_inbox(vault: str, context: ToolContext = None) -> str:
    assert context is not None
    vp = resolve_vault(vault, context)
    inbox = vp / "inbox"
    if not inbox.is_dir():
        return f"ERROR: no inbox at {inbox}"
    units = []
    for e in sorted(inbox.iterdir()):
        if e.name == "_skipped" or e.name.startswith("."):
            continue
        units.append(f"  {e.name}  -- {'PROJECT (folder)' if e.is_dir() else 'standalone file'}")
    if not units:
        return "INBOX EMPTY"
    return f"{len(units)} unit(s) in {inbox} -- process ONE per cycle:\n" + "\n".join(units)


def wiki_search(vault: str, query: str, context: ToolContext = None) -> str:
    assert context is not None
    query = text_or_default(query)
    vp = resolve_vault(vault, context)
    rc, out, err = run(["qmd", "query", query, "-c", collection_for(vp)], context)
    wiki_say(f"search \"{query[:40]}\"", context)
    return out.strip() or err.strip() or "(no results)"


def wiki_archive(vault: str, unit: str, context: ToolContext = None) -> str:
    assert context is not None
    vp = resolve_vault(vault, context)
    src = vp / "inbox" / unit
    if not src.exists():
        return f"ERROR: no inbox unit: {src}"
    # Leave-in-place mode (js-drain default): report success without moving so
    # the close-out still logs/commits, but the raw stays where the caller put it.
    if getattr(context, "wiki_no_archive", False) or os.environ.get("JS_WIKI_NO_ARCHIVE"):
        return f"archive skipped (leave-in-place): inbox/{unit} kept"
    clip = vp / "Clippings"
    clip.mkdir(exist_ok=True)
    dest = clip / unit
    if dest.exists():
        return f"ERROR: {dest} already archived -- not overwriting."
    try:
        shutil.move(str(src), str(dest))
    except OSError as exc:
        return f"ERROR: {exc}"
    wiki_say(f"{C.BR_YELLOW}archived{C.RESET} {unit} → Clippings", context)
    return f"archived: inbox/{unit} -> Clippings/{unit}  (done-marker set)"


def wiki_log(vault: str, op: str, title: str, note: str = "", context: ToolContext = None) -> str:
    assert context is not None
    vp = resolve_vault(vault, context)
    log = vp / "log.md"
    entry = f"\n## [{today()}] {op} | {title}\n{note}\n"
    try:
        with vault_lock(vp, getattr(context, "wiki_vault_lock_timeout_s", 30)):
            with log.open("a", encoding="utf-8") as fh:
                fh.write(entry)
    except OSError as exc:
        return f"ERROR: {exc}"
    wiki_say(f"logged {op}: {title}", context)
    return f"logged: [{today()}] {op} | {title}"


def _maybe_git_commit(vault_path, message: str, context: ToolContext) -> str:
    """If the vault is a git repo, stage everything and commit. Silent skip on
    non-repo. Returns a short status line for the ingest report."""
    if not (vault_path / ".git").exists():
        return "git: (no repo — skipping auto-commit)"
    with vault_lock(vault_path, getattr(context, "wiki_vault_lock_timeout_s", 30)):
        rc1, _, err1 = run(["git", "-C", str(vault_path), "add", "-A"], context)
        if rc1 != 0:
            return f"git add failed: {err1.strip()}"
        # Skip the commit if there's nothing staged (shouldn't happen post-ingest,
        # but defensive). `git diff --cached --quiet` returns 0 = no changes.
        rc_diff, _, _ = run(["git", "-C", str(vault_path), "diff", "--cached", "--quiet"], context)
        if rc_diff == 0:
            return "git: nothing to commit"
        rc2, out2, err2 = run(
            ["git", "-C", str(vault_path), "commit", "-m", message], context
        )
        if rc2 != 0:
            return f"git commit failed: {(err2 or out2).strip()}"
        rc3, sha, _ = run(["git", "-C", str(vault_path), "rev-parse", "--short", "HEAD"], context)
        sha = sha.strip() if rc3 == 0 else "?"
        return f"git: committed {sha}"


def wiki_finish_ingest(vault: str, unit: str, title: str, note: str = "",
                       context: ToolContext = None) -> str:
    """ATOMIC close-out for one ingest cycle: archive then log in a single tool
    call, then auto-commit to git if the vault is a repo. Prevents the failure
    mode where the model writes pages, logs, then drops wiki_archive and
    leaves the raw stranded in inbox/. Archive runs FIRST — if it fails
    (already-archived / missing), nothing is logged or committed.

    Per-unit git commits mean a bad ingest can be reverted with `git revert`.
    Auto-commit is a no-op on non-repo vaults — opt in by running
    `git init` in the vault root."""
    assert context is not None
    vp = resolve_vault(vault, context)
    archive_result = wiki_archive(vault, unit, context)
    if archive_result.startswith("ERROR"):
        return f"NOT logging — archive failed: {archive_result}"
    archive_note = (
        f"Left in place: inbox/{unit}"
        if archive_result.startswith("archive skipped")
        else f"Archived: {unit} -> Clippings/{unit}"
    )
    combined_note = f"{note}\n\n{archive_note}".strip()
    log_result = wiki_log(vault, "ingest", title, combined_note, context)
    if (getattr(context, "wiki_mode", "") or os.environ.get("JS_WIKI_MODE", "")) == "ingest":
        git_result = "git: deferred (ingest mode — synthesize/drain commits)"
    else:
        git_result = _maybe_git_commit(vp, f"ingest: {title}", context)
    wiki_say(f"{C.BR_GREEN}finished ingest{C.RESET} {title}  ({git_result})", context)
    return f"{archive_result}\n{log_result}\n{git_result}"


def wiki_commit(vault: str, message: str, context: ToolContext = None) -> str:
    """Stage and commit the vault explicitly. Useful at start of a run to
    snapshot the pre-run state, or to checkpoint mid-run between phases.
    No-op if the vault isn't a git repo. Skips empty commits silently."""
    assert context is not None
    vp = resolve_vault(vault, context)
    return _maybe_git_commit(vp, message, context)
