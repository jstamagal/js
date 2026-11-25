"""The REPL's workspace commands: /cd, /add and /drop, where the session
works. Each change queues one reminder for the next user message and records
a `workspace:` mark, so a resumed session works in the same place
(`restore_workspace`)."""

from __future__ import annotations

import os
from pathlib import Path

from . import (
    jail as _jail,
    memory as M,
    messages as msgs,
    repl_state,
    runtime,
    settings,
    snippets,
)
from .config import Config
from .repl_state import Command, ReplState


def _access(bind: _jail.Bind) -> str:
    return "read-write" if bind.rw else "read-only"


def set_working_dir(path: Path) -> None:
    os.chdir(path)
    runtime.T.STOCK_CONTEXT.cwd = path


def _record_workspace(cfg: Config) -> None:
    if cfg.session_file == Path(os.devnull):
        return
    jail = _jail.active()
    M.append_workspace_mark(
        cfg.session_file,
        root=str(jail.root) if jail is not None else None,
        cwd=str(runtime.T.STOCK_CONTEXT.cwd),
        binds=[bind.spec() for bind in jail.added] if jail is not None else [],
    )


def _cmd_cd(arg: str, state: ReplState, cfg: Config) -> str | None:
    context = runtime.T.STOCK_CONTEXT
    raw = arg.strip()
    if not raw:
        msgs.say(msgs.CWD_IS, path=context.cwd)
        return None
    target = Path(os.path.expanduser(raw))
    target = (target if target.is_absolute() else context.cwd / target).resolve()
    if not target.is_dir():
        return msgs.CD_NOT_A_DIR.said(path=target)
    jail = _jail.active()
    if jail is not None and not jail.bound(target, settings.knob(state.settings, "JAIL_BIND")):
        return msgs.CD_OUTSIDE_JAIL.said(path=target, root=jail.root)
    set_working_dir(target)
    repl_state.queue_note(state, snippets.render("working-directory", PATH=target))
    _record_workspace(cfg)
    msgs.say(msgs.CD_DONE, path=target)
    return None


def _cmd_add(arg: str, state: ReplState, cfg: Config) -> str | None:
    jail = _jail.active()
    if jail is None:
        return msgs.NO_JAIL.said(verb="add")
    spec = arg.strip()
    if spec and not spec.startswith(("/", "~")):
        spec = str(runtime.T.STOCK_CONTEXT.cwd / spec)
    try:
        bind = _jail.parse_bind(spec)
    except ValueError as exc:
        return msgs.ADD_BAD.said(error=exc)
    if not bind.path.exists():
        return msgs.ADD_MISSING.said(path=bind.path)
    jail.add(bind)
    repl_state.queue_note(state, snippets.render("jail-path-added", PATH=bind.path, ACCESS=_access(bind)))
    _record_workspace(cfg)
    msgs.say(msgs.ADD_DONE, path=bind.path, access=_access(bind))
    return None


def _cmd_drop(arg: str, state: ReplState, cfg: Config) -> str | None:
    jail = _jail.active()
    if jail is None:
        return msgs.NO_JAIL.said(verb="drop")
    raw = arg.strip()
    path = Path(os.path.abspath(runtime.T.STOCK_CONTEXT.cwd / os.path.expanduser(raw)))
    if path.resolve() == jail.root:
        return msgs.DROP_ROOT.said(path=path)
    dropped = next((bind for bind in jail.added if bind.path == path), None)
    if dropped is None:
        return msgs.DROP_UNKNOWN.said(path=path)
    jail.drop(path)
    if not jail.bound(runtime.T.STOCK_CONTEXT.cwd, settings.knob(state.settings, "JAIL_BIND")):
        jail.add(dropped)
        return msgs.DROP_CWD.said(path=path)
    repl_state.queue_note(state, snippets.render("jail-path-removed", PATH=path))
    _record_workspace(cfg)
    msgs.say(msgs.DROP_DONE, path=path)
    return None


def restore_workspace(cfg: Config) -> None:
    """Put a resumed session back where its last /cd, /add or /drop left it.
    The /add binds come back only under a jail with the same -C root; the
    working directory only where the jail shows it."""
    mark = M.last_workspace(cfg.session_file)
    if mark is None:
        return
    jail = _jail.active()
    if jail is not None and mark.get("root") == str(jail.root):
        for spec in mark.get("binds") or ():
            try:
                bind = _jail.parse_bind(spec)
            except ValueError:
                continue
            if bind.path.exists():
                jail.add(bind)
    cwd = Path(str(mark.get("cwd") or ""))
    setting = settings.knob(getattr(cfg, "settings", None), "JAIL_BIND")
    if cwd.is_absolute() and cwd.is_dir() and (jail is None or jail.bound(cwd, setting)):
        set_working_dir(cwd)


COMMANDS: dict[str, Command] = {
    "cd": Command(_cmd_cd, "cd [dir]", msgs.CMD_CD, complete="path", turn_state=True),
    "add": Command(_cmd_add, "add <path>[:rw]", msgs.CMD_ADD, complete="path"),
    "drop": Command(_cmd_drop, "drop <path>", msgs.CMD_DROP, complete="path", turn_state=True),
}
