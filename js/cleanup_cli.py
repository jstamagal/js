"""`js cleanup [DIR ...]`: run the rules of `js.cleanup` over each workspace,
then, only where something needs reading, the stock `cleanup` agent: it
describes the pieces of work archived without a description, and settles
the source checkouts the rules could not. The agent runs as `js -C DIR`,
kept in the workspace, with the workspace's archive area shown read-write
and nothing else of the home.

The workspaces are DIR, else the `CLEANUP_DIRS` setting, else the home's
work directory."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import cleanup, jail, messages as msgs, paths as _paths, settings, snippets
from .config import run_settings

AGENT = "cleanup"


def parser():
    p = msgs.ArgumentParser(prog="js cleanup", allow_abbrev=False, description=msgs.CLEANUP_DESCRIPTION.words())
    p.add_argument("directories", nargs="*", metavar="DIR", help=msgs.CLEANUP_OPT_DIR.words())
    p.add_argument("--dry-run", action="store_true", help=msgs.CLEANUP_OPT_DRY_RUN.words())
    p.add_argument("--no-model", action="store_true", help=msgs.CLEANUP_OPT_NO_MODEL.words())
    p.add_argument("-m", "--model", help=msgs.CLEANUP_OPT_MODEL.words())
    p.add_argument("-r", "--reasoning", help=msgs.CLEANUP_OPT_REASONING.words())
    return p


def _ages(live: dict) -> cleanup.Ages:
    return cleanup.Ages(
        archive=float(settings.knob(live, "CLEANUP_ARCHIVE_AGE_MAX")),
        gzip=float(settings.knob(live, "CLEANUP_GZIP_AGE_MAX")),
        tar=float(settings.knob(live, "CLEANUP_TAR_AGE_MAX")),
    )


# --- the jail of the agent's run ------------------------------------------


def _gitdir(dot_git: Path) -> Path | None:
    """The administrative directory a `.git` file names, absolute."""
    try:
        first = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    except OSError:
        return None
    if not first or not first[0].startswith("gitdir:"):
        return None
    named = Path(first[0].removeprefix("gitdir:").strip())
    return jail.real_path(named if named.is_absolute() else dot_git.parent / named)


def worktree_binds(directories: list[Path], workspace: Path) -> list[str]:
    """`JAIL_BIND` entries that let git work in the linked worktrees under
    ``directories`` whose repository is outside ``workspace``: the
    repository (its working tree when it has one) read-only, so the agent can
    compare a checkout with the owner's copy, and its `.git/worktrees`
    directory read-write, so `git worktree remove` can retire it."""
    root = jail.real_path(workspace)
    binds: dict[str, None] = {}
    for directory in directories:
        for current, dirs, files in os.walk(directory):
            dirs[:] = [name for name in dirs if name != ".git"]
            if ".git" not in files:
                continue
            gitdir = _gitdir(Path(current) / ".git")
            if gitdir is None or not gitdir.is_dir() or gitdir.is_relative_to(root):
                continue
            try:
                named = Path((gitdir / "commondir").read_text(encoding="utf-8").strip())
                common = jail.real_path(named if named.is_absolute() else gitdir / named)
            except OSError:
                common = gitdir
            repository = common.parent if common.name == ".git" else common
            binds[str(repository)] = None
            # The directory of worktree entries, not the entry itself: a bind
            # mount point cannot be removed from inside the jail, and
            # `git worktree remove` removes the entry.
            admin = gitdir.parent if gitdir.parent.name == "worktrees" else gitdir
            binds[f"{admin}:rw"] = None
    return list(binds)


def _hides_js_home(entry: object) -> bool:
    try:
        path = jail.parse_hide(str(entry))
    except ValueError:
        return False
    home = jail.real_path(_paths.home())
    return jail.real_path(path) == home or (path == _paths.default_home())


def jail_extras(workspace: Path, area: Path, hide: object, bind: object, *, checkouts: list[Path]) -> list[str]:
    """The `--extra` values that set the agent's jail: the js home's
    `JAIL_HIDE` entry left out (the archive area is inside the js home, and
    the work directory may be), the archive area and the one log shown
    read-write, and the repositories of the unsettled checkouts shown as
    `worktree_binds` says."""
    hides = [entry for entry in (hide if isinstance(hide, (list, tuple)) else []) if not _hides_js_home(entry)]
    binds = list(bind if isinstance(bind, (list, tuple)) else [])
    for entry in [f"{area}:rw", f"{cleanup.log_file()}:rw", *worktree_binds(checkouts, workspace)]:
        if entry not in binds:
            binds.append(entry)
    return [f"JAIL_HIDE={json.dumps(hides)}", f"JAIL_BIND={json.dumps(binds)}"]


def task(run: cleanup.Cleanup, undescribed: list[Path]) -> str:
    """The agent's prompt: the pieces to describe and the checkouts to settle."""
    pieces = "\n".join(f"- {path} ({run._stage(path)})" for path in undescribed) or "- none"
    checkouts = "\n".join(f"- {path}" for path in run.report.checkouts) or "- none"
    return snippets.render("cleanup-task", WORKSPACE=run.workspace, ARCHIVE=run.area,
                           DESCRIPTIONS=run.area / cleanup.DESCRIPTIONS, LOG=run.log,
                           PIECES=pieces, CHECKOUTS=checkouts)


def agent_argv(run: cleanup.Cleanup, prompt: str, extras: list[str], *, model: str | None,
               reasoning: str | None) -> list[str]:
    """The `js` command line of the agent's run over ``run``'s workspace."""
    argv = [sys.executable, "-m", "js", "--home", str(_paths.home()), "-C", str(run.workspace),
            "-a", AGENT, "--no-agents-md", "--no-local-config", "-q", "-p", prompt]
    for extra in extras:
        argv += ["--extra", extra]
    if model:
        argv += ["-m", model]
    if reasoning:
        argv += ["-r", reasoning]
    return argv


# --- the command -----------------------------------------------------------


def _run_agent(argv: list[str]) -> int:
    """Run the agent's `js` command line; its exit code."""
    try:
        return subprocess.run(argv, check=False, stdin=subprocess.DEVNULL).returncode
    except OSError:
        return 1


def _say_report(report: cleanup.Report, *, dry: bool) -> None:
    msgs.say(msgs.CLEANUP_WORKSPACE, workspace=report.workspace, archive=report.archive,
             mode=" (dry run)" if dry else "")
    msgs.say(msgs.CLEANUP_TOTALS, freed=cleanup._human(report.freed), removed=report.removed,
             moved=report.moved, archived=len(report.archived), gzipped=len(report.gzipped),
             tarred=len(report.tarred), patches=len(report.patches))
    for path, reason in report.left:
        msgs.say(msgs.CLEANUP_LEFT, path=path, reason=reason)
    for path, error in report.failed:
        msgs.say(msgs.CLEANUP_FAILED, path=path, error=error)
    if dry:
        for line in report.lines:
            print(line)


def _describe(run: cleanup.Cleanup, live: dict, args) -> None:
    """Run the agent where something needs reading; say what it left when it
    could not run."""
    undescribed = run.undescribed()
    if not undescribed and not run.report.checkouts:
        return
    # The agent's jail shows the log only when it is there to bind.
    run.log.parent.mkdir(parents=True, exist_ok=True)
    run.log.touch()
    extras = jail_extras(run.workspace, run.area, settings.knob(live, "JAIL_HIDE"), settings.knob(live, "JAIL_BIND"),
                         checkouts=run.report.checkouts)
    code = _run_agent(agent_argv(run, task(run, undescribed), extras, model=args.model, reasoning=args.reasoning))
    run.asked(run.report.checkouts)
    if code:
        msgs.say(msgs.CLEANUP_AGENT_FAILED, code=code, pieces=len(undescribed), checkouts=len(run.report.checkouts))
    run.write_indexes()


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        live = run_settings()
        ages = _ages(live)
        named = [Path(item).expanduser().resolve() for item in args.directories]
        targets = named or cleanup.workspaces(settings.knob(live, "CLEANUP_DIRS") or [])
    except (ValueError, OSError) as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    code = 0
    for workspace in targets:
        if not workspace.is_dir():
            msgs.warn(msgs.CLEANUP_NOT_A_DIRECTORY, directory=workspace)
            code = 2
            continue
        run = cleanup.Cleanup(workspace, ages, dry=args.dry_run)
        try:
            report = run.run()
        except OSError as exc:
            msgs.warn(msgs.FAILED, error=exc)
            code = 1
            continue
        if not args.dry_run and not args.no_model:
            _describe(run, live, args)
        _say_report(report, dry=args.dry_run)
        if report.failed:
            code = 1
    return code
