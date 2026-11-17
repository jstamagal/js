"""`js --commit`: snapshot the worktree, then run the commit agent over it."""

from __future__ import annotations

import contextlib
import io
import os
import shlex
import shutil
from datetime import UTC, datetime
from pathlib import Path

from . import (
    messages as msgs,
    paths as _paths,
    prompt_run,
    snippets,
)
from .config import (
    InvocationSources,
)
from .toolkit import ToolContext


def _commit_backup_root() -> Path:
    return _paths.commit_backups_dir()


def _worktree_patch(repo_dir: Path, commit_helper) -> str:
    """The unified diff of every tracked uncommitted change.

    `git diff HEAD` is staged+unstaged against the last commit. A repo with no
    commits yet has no HEAD, so fall back to staged+unstaged separately.
    """
    flags = (*commit_helper._DIFF_FLAGS, "--binary")
    diff = commit_helper._git("diff", *flags, "HEAD", check=False, repo=repo_dir)
    if diff.returncode == 0:
        return diff.stdout
    staged = commit_helper._git("diff", "--cached", *flags, check=False, repo=repo_dir)
    unstaged = commit_helper._git("diff", *flags, check=False, repo=repo_dir)
    return (staged.stdout if staged.returncode == 0 else "") + (unstaged.stdout if unstaged.returncode == 0 else "")


def _prune_commit_backups(root: Path, keep: int = 10) -> None:
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda p: p.name)
    for old in dirs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def _snapshot_worktree(repo_dir: Path) -> Path | None:
    """Copy all uncommitted work into a timestamped backup before the agent touches the tree.

    The commit agent can revert files while splitting commits; a snapshot means any
    failure path is recoverable. Returns the backup dir, or None on a clean tree.
    """
    import hashlib

    from . import commit_helper

    patch = _worktree_patch(repo_dir, commit_helper)
    others = commit_helper._git("ls-files", "--others", "--exclude-standard", "-z", check=False, repo=repo_dir)
    untracked = [p for p in others.stdout.split("\0") if p] if others.returncode == 0 else []
    if not patch.strip() and not untracked:
        return None

    repo_key = hashlib.sha256(os.fsencode(repo_dir.resolve())).hexdigest()
    repo_backups = _commit_backup_root() / repo_key
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = repo_backups / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    if patch:
        (backup_dir / "tracked.patch").write_bytes(patch.encode("utf-8", "surrogateescape"))
    for rel in untracked:
        dest = backup_dir / "untracked" / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo_dir / rel, dest, follow_symlinks=False)
        except OSError:
            continue  # a vanished/unreadable file must not abort the whole snapshot
    _prune_commit_backups(repo_backups)
    return backup_dir


def run_commit(target: str | None,
                model: str | None = None, debug: bool = False, debug_file: str | None = None,
                session: str | None = None, save: bool = True,
                reasoning: str | None = None, maxout: int | None = None,
                extra_context: str | None = None,
                sources: InvocationSources = InvocationSources()) -> int:
    target_dir = Path(target).expanduser() if target else Path.cwd()
    if not target_dir.is_absolute():
        target_dir = Path.cwd() / target_dir
    target_dir = target_dir.resolve(strict=False)
    if not target_dir.exists():
        msgs.warn(msgs.COMMIT_TARGET_MISSING, path=target_dir)
        return 2
    if not target_dir.is_dir():
        msgs.warn(msgs.COMMIT_TARGET_NOT_DIR, path=target_dir)
        return 2

    from . import commit_helper

    probe = commit_helper._git("rev-parse", "--is-inside-work-tree", check=False, repo=target_dir)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        init = commit_helper._git("init", "-q", "-b", "main", check=False, repo=target_dir)
        if init.returncode != 0:
            init = commit_helper._git("init", "-q", check=False, repo=target_dir)
        if init.returncode != 0:
            detail = (init.stderr or init.stdout).strip() or f"exit {init.returncode}"
            msgs.warn(msgs.GIT_INIT_FAILED, path=target_dir, error=detail)
            return 1

    try:
        backup_dir = _snapshot_worktree(target_dir)
    except Exception as e:  # noqa: BLE001 — a snapshot failure must never block the commit run
        msgs.warn(msgs.SNAPSHOT_FAILED, error=f"{type(e).__name__}: {e}")
    else:
        if backup_dir is not None:
            msgs.say(msgs.SNAPSHOT_SAVED, path=backup_dir)

    survey_out = io.StringIO()
    survey_err = io.StringIO()
    with contextlib.redirect_stdout(survey_out), contextlib.redirect_stderr(survey_err):
        survey_rc = commit_helper.main(["-C", str(target_dir), "survey"])
    survey = survey_out.getvalue().rstrip()
    if survey_rc != 0:
        detail = survey_err.getvalue().strip() or survey or msgs.SURVEY_EXITED.said(code=survey_rc)
        msgs.warn(msgs.SURVEY_FAILED, path=target_dir, error=detail)
        return 1

    helper_stage = f'python -m js.commit_helper -C {shlex.quote(str(target_dir))} stage <file> <hunks|all>'
    helper_survey = f'python -m js.commit_helper -C {shlex.quote(str(target_dir))} survey'
    helper_commit = f'python -m js.commit_helper -C {shlex.quote(str(target_dir))} commit <message-file>'
    prompt = (
        snippets.render('commit-task', DIRECTORY=target_dir, STAGE=helper_stage, COMMIT=helper_commit, SURVEY_COMMAND=helper_survey, SURVEY=survey)
    )
    if extra_context and extra_context.strip():
        prompt += f'\n\nOperator context:\n{extra_context.strip()}'

    return prompt_run.run(
        prompt,
        model=model,
        debug=debug,
        debug_file=debug_file,
        agent="commit",
        session=session,
        save=save,
        resume_prefix=f"js --commit {target_dir}",
        reasoning=reasoning,
        maxout=maxout,
        sources=sources,
        config_pins=True,
        tool_context=ToolContext(cwd=target_dir),
    )

