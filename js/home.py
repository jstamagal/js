"""Move the pre-`~/.js` locations into `~/.js`, and sweep `~/.js/tmp`.

    uv run python -m js.home [--apply]

Without --apply it prints what it would move. `migrate_once()` runs the same
move at js startup, once: the marker `paths.home_migration_marker()` records
that it ran, so later starts do nothing even if an old location reappears.

The old agent inbox moves whole to ~/.js/work. Of the old config and data
directories, only the entries js reads move, each to the place js reads it
from now (`_CONFIG_ENTRIES`, `_DATA_ENTRIES`). Every other entry is left in
place with one line naming it, and its old directory is then not removed.

After the moves, every agent in ~/.js/agents is converted to agent.yaml
(`js.agent_migration`), and the sessions of each old per-agent folder
(`sessions/<agent>/`) are filed by the directory they started in
(`js.session_store`): a session goes to the folder of the cwd in its first
start metadata, or of ~ when it has none; an old subagent run, which has no
metadata, goes under the parent session whose task call carried its first
message. Names are kept, so `--session <old name or hash tail>` still
resolves. Each filed session is rewritten once to give every record an id
and a parent, the format js writes today (`js.session_store`). A session
without an agent in its metadata then gets a start record naming the old
folder's agent, and each filed session gets its `.txt`. The
old folder's `.history` and `latest.json` go to ~/.js/state/<agent>/.

Every move is a rename of one entry, so a directory lands whole or not at
all. A symlink is moved as the link; the walk never descends through one.
When the destination already exists: an identical file or link drops the
source copy, a directory is merged entry by entry, and anything else is
refused with the reason and left where it was, as is an entry that cannot
be read or compared; the walk goes on to the next. Across filesystems an entry
is copied beside its destination, renamed into place, and only then removed
from the source.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import filecmp
import json
import os
import shutil
import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from . import agent_migration
from . import messages as msgs
from . import paths
from . import session_catalog
from . import session_store
from . import session_text

# The entries of the old config and data directories js reads, each with the
# place it reads it from now. Every other entry is left where it is.
_CONFIG_ENTRIES = {
    "jsrc": paths.global_config_file,
    "config.toml": paths.legacy_global_config_file,
    "JS.md": lambda: paths.global_instruction_files()[0],
    "JS.local.md": lambda: paths.global_instruction_files()[1],
    "agents": paths.global_agents_dir,
    "skills": paths.global_skills_dir,
    "toolbox": paths.global_toolbox_dir,
    "tools.yaml": paths.tools_config_file,
    ".env": paths.global_env_file,
    "logins.toml": lambda: paths.login_store_dir() / "logins.toml",
    "models-cache.json": lambda: paths.login_store_dir() / "models-cache.json",
}
_DATA_ENTRIES = {
    "sessions": paths.sessions_root,
    "state": paths.state_root,
    "logs": paths.logs_root,
    "transcript": paths.transcript_root,
    "modelsdotdev": paths.model_catalog_dir,
    "notes": paths.notes_dir,
    "commit-backups": paths.commit_backups_dir,
}

# `~/.js/tmp` entries untouched for this long are removed at startup.
TMP_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Step:
    kind: str  # "move", "duplicate", "refuse", "rmdir", "relink", "convert", "drop", "skip", "refile", "unused"
    source: Path
    target: Path | None = None
    reason: str = ""


def _short(path: Path) -> str:
    text = str(path)
    home = str(paths.user_home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def describe(step: Step, *, apply: bool) -> msgs.Said:
    source, target = _short(step.source), _short(step.target) if step.target else ""
    entry = {
        ("move", True): msgs.HOME_MOVED, ("move", False): msgs.HOME_WOULD_MOVE,
        ("duplicate", True): msgs.HOME_REMOVED_DUPLICATE, ("duplicate", False): msgs.HOME_WOULD_REMOVE_DUPLICATE,
        ("rmdir", True): msgs.HOME_REMOVED_EMPTY, ("rmdir", False): msgs.HOME_WOULD_REMOVE_EMPTY,
        ("relink", True): msgs.HOME_RELINKED, ("relink", False): msgs.HOME_WOULD_RELINK,
        ("convert", True): msgs.HOME_CONVERTED, ("convert", False): msgs.HOME_WOULD_CONVERT,
        ("drop", True): msgs.HOME_DROPPED, ("drop", False): msgs.HOME_WOULD_DROP,
        ("skip", True): msgs.HOME_LEFT, ("skip", False): msgs.HOME_WOULD_LEAVE,
        ("refile", True): msgs.HOME_REFILED, ("refile", False): msgs.HOME_WOULD_REFILE,
        ("unused", True): msgs.HOME_UNUSED, ("unused", False): msgs.HOME_WOULD_LEAVE_UNUSED,
    }.get((step.kind, apply)) or (msgs.HOME_REFUSED if apply else msgs.HOME_WOULD_REFUSE)
    return entry.said(source=source, target=target, reason=step.reason)


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _same(source: Path, target: Path, source_mode: int, target_mode: int) -> bool:
    if stat.S_ISLNK(source_mode) and stat.S_ISLNK(target_mode):
        return os.readlink(source) == os.readlink(target)
    if stat.S_ISREG(source_mode) and stat.S_ISREG(target_mode):
        return filecmp.cmp(source, target, shallow=False)
    return False


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return msgs.HOME_KIND_LINK.text()
    if stat.S_ISDIR(mode):
        return msgs.HOME_KIND_DIR.text()
    return msgs.HOME_KIND_FILE.text()


class _OldCopyLeft(OSError):
    """The entry is in place at its target; removing the old copy failed."""


def _move_across(source: Path, target: Path, mode: int) -> None:
    """Copy beside the target, rename into place, then remove the source."""
    staging = target.with_name(f".{target.name}.migrating-{os.getpid()}")
    try:
        if stat.S_ISLNK(mode):
            os.symlink(os.readlink(source), staging)
        elif stat.S_ISDIR(mode):
            shutil.copytree(source, staging, symlinks=True)
        else:
            shutil.copy2(source, staging, follow_symlinks=False)
        os.rename(staging, target)
    except BaseException:
        if os.path.lexists(staging):
            if os.path.isdir(staging) and not os.path.islink(staging):
                shutil.rmtree(staging, ignore_errors=True)
            else:
                os.unlink(staging)
        raise
    try:
        if stat.S_ISDIR(mode):
            shutil.rmtree(source)
        else:
            os.unlink(source)
    except OSError as exc:
        raise _OldCopyLeft(exc.errno, str(exc)) from exc


def _rename(source: Path, target: Path, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _move_across(source, target, mode)


def _links_under(root: Path, mode: int) -> list[Path]:
    """`root` itself when it is a symlink, else every symlink below it; never
    descends through one."""
    if stat.S_ISLNK(mode):
        return [root]
    if not stat.S_ISDIR(mode):
        return []
    found = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        found.extend(Path(directory) / name for name in (*dirnames, *filenames)
                     if os.path.islink(os.path.join(directory, name)))
    return found


def _inside(path: str, root: Path) -> bool:
    return path == str(root) or path.startswith(str(root) + os.sep)


def _relinks(source: Path, target: Path, mode: int, *, apply: bool) -> Iterator[Step]:
    """Point each relative symlink the move carried at the absolute target it
    resolved to before the move. A link to something that moved with it still
    resolves to it and is left alone."""
    where = target if apply else source
    try:
        links = _links_under(where, mode)
    except OSError:
        return
    for link in links:
        relative = link.relative_to(where)
        old_link, new_link = source / relative, target / relative
        try:
            text = os.readlink(link)
        except OSError:
            continue
        if os.path.isabs(text):
            continue
        before = os.path.normpath(os.path.join(old_link.parent, text))
        if not stat.S_ISLNK(mode) and _inside(before, source):
            continue
        if os.path.normpath(os.path.join(new_link.parent, text)) == before:
            continue
        if apply:
            staging = new_link.with_name(f".{new_link.name}.relink-{os.getpid()}")
            try:
                os.symlink(before, staging)
                os.replace(staging, new_link)
            except OSError as exc:
                with contextlib.suppress(OSError):
                    os.unlink(staging)
                yield Step("refuse", new_link, Path(before), msgs.HOME_NOT_MOVED.text(target=before, error=exc))
                continue
        yield Step("relink", new_link if apply else old_link, Path(before))


class _Walk:
    """One pass over the old locations, performed or only planned.

    A dry run records each planned move, so a later entry whose target lies
    at or under a planned target is checked against what would be there: the
    planned source. The dry run then reports the same merges and refusals the
    real run meets.
    """

    def __init__(self, *, apply: bool) -> None:
        self.apply = apply
        self.planned: dict[Path, Path] = {}

    def where(self, target: Path) -> Path:
        """The path whose contents `target` holds once the moves before it are done."""
        for candidate in (target, *target.parents):
            source = self.planned.get(candidate)
            if source is not None:
                return source / target.relative_to(candidate)
        return target

    def entry(self, source: Path, target: Path) -> Iterator[Step]:
        """Move one entry to `target`, merging into an existing directory there."""
        try:
            source_stat = _lstat(source)
            target_stat = _lstat(self.where(target))
        except OSError as exc:
            yield Step("refuse", source, target, msgs.HOME_NOT_EXAMINED_WITH.text(target=_short(target), error=exc))
            return
        if source_stat is None:
            return
        if target_stat is None:
            yield from self._move(source, target, source_stat.st_mode)
            return
        source_mode, target_mode = source_stat.st_mode, target_stat.st_mode
        if stat.S_ISDIR(source_mode) and stat.S_ISDIR(target_mode):
            try:
                children = sorted(os.listdir(source))
            except OSError as exc:
                yield Step("refuse", source, target, msgs.HOME_NOT_LISTED.text(error=exc))
                return
            for child in children:
                yield from self.entry(source / child, target / child)
            yield from self.remove_if_empty(source)
            return
        try:
            same = _same(source, self.where(target), source_mode, target_mode)
        except OSError as exc:
            yield Step("refuse", source, target, msgs.HOME_NOT_COMPARED.text(target=_short(target), error=exc))
            return
        if same:
            if self.apply:
                try:
                    os.unlink(source)
                except OSError as exc:
                    yield Step("refuse", source, target, msgs.HOME_DUPLICATE_NOT_REMOVED.text(target=_short(target), error=exc))
                    return
            yield Step("duplicate", source, target)
            return
        if stat.S_ISREG(source_mode) and stat.S_ISREG(target_mode):
            reason = msgs.HOME_TARGET_DIFFERS.text(target=_short(target))
        else:
            reason = msgs.HOME_TARGET_OTHER_KIND.text(target=_short(target), kind=_kind(target_mode),
                                                       source_kind=_kind(source_mode))
        yield Step("refuse", source, target, reason)

    def _move(self, source: Path, target: Path, mode: int) -> Iterator[Step]:
        if not self.apply:
            self.planned[target] = source
            yield Step("move", source, target)
            yield from _relinks(source, target, mode, apply=False)
            return
        try:
            _rename(source, target, mode)
        except _OldCopyLeft as exc:
            yield Step("move", source, target)
            yield Step("refuse", source, target, msgs.HOME_OLD_COPY_LEFT.text(target=_short(target), error=exc))
            yield from _relinks(source, target, mode, apply=True)
            return
        except OSError as exc:
            yield Step("refuse", source, target, msgs.HOME_NOT_MOVED.text(target=_short(target), error=exc))
            return
        yield Step("move", source, target)
        yield from _relinks(source, target, mode, apply=True)

    def remove_if_empty(self, directory: Path) -> Iterator[Step]:
        if not self.apply:
            return
        try:
            os.rmdir(directory)
        except OSError:
            return
        yield Step("rmdir", directory)

    def spread(self, root: Path, entries: dict) -> Iterator[Step]:
        """Move each entry of an old directory that `entries` names to its place
        under ~/.js; leave every other entry where it is, one step each."""
        try:
            root_stat = _lstat(root)
        except OSError as exc:
            yield Step("refuse", root, None, msgs.HOME_NOT_EXAMINED.text(error=exc))
            return
        if root_stat is None:
            return
        if not stat.S_ISDIR(root_stat.st_mode):
            yield Step("refuse", root, None, msgs.HOME_NOT_A_DIR.text(kind=_kind(root_stat.st_mode)))
            return
        try:
            names = os.listdir(root)
        except OSError as exc:
            yield Step("refuse", root, None, msgs.HOME_NOT_LISTED.text(error=exc))
            return
        for name in sorted(name for name in names if name not in entries):
            yield Step("unused", root / name)
        # Entries that land directly in ~/.js go first, so a directory another
        # entry moves into (state/, logs/) arrives whole before it is added to.
        targets = {name: entries[name]() for name in names if name in entries}
        for name in sorted(targets, key=lambda name: (targets[name].parent != paths.home(), name)):
            yield from self.entry(root / name, targets[name])
        yield from self.remove_if_empty(root)


def _agent_step(result: agent_migration.Result) -> Iterator[Step]:
    if result.action == "migrate":
        yield Step("convert", result.agent_dir, None, result.message)
    elif result.action == "skip":
        yield Step("skip", result.agent_dir, None, result.message)
    elif result.action == "error":
        yield Step("refuse", result.agent_dir, None, result.message)
    if result.dropped:
        yield Step("drop", result.agent_dir, None, ", ".join(result.dropped))


def convert_agents(*, apply: bool) -> Iterator[Step]:
    """Convert every agent in ~/.js/agents to agent.yaml, dropping tools entries
    that match no tool. A dry run looks where the agents are before the moves."""
    roots = [paths.global_agents_dir()]
    if not apply:
        roots.insert(0, paths.legacy_homes()["config"] / "agents")
    roots = [root for root in roots if root.is_dir() and not root.is_symlink()]
    if not roots:
        return
    keep = agent_migration.tool_matcher(agent_migration.default_roots(*roots))
    for root in roots:
        try:
            results = agent_migration.migrate_root(root, apply=apply, keep=keep)
        except OSError as exc:
            yield Step("refuse", root, None, msgs.HOME_NOT_LISTED.text(error=exc))
            continue
        for result in results:
            yield from _agent_step(result)


@dataclass
class _OldSession:
    """One session file of an old per-agent folder, as far as filing needs it."""

    path: Path
    folder: Path
    metadata: dict | None = None
    has_agent: bool = False
    first_user: str | None = None
    first_ts: float | None = None
    tasks: list[tuple[float, str]] = field(default_factory=list)

    @property
    def relative(self) -> Path:
        return self.path.relative_to(self.folder)


def _task_texts(arguments) -> list[str]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return []
    items = arguments.get("tasks") if isinstance(arguments, dict) else None
    return [item.strip() for item in items if isinstance(item, str) and item.strip()] if isinstance(items, list) else []


def _read_old_session(folder: Path, path: Path) -> _OldSession:
    session = _OldSession(path, folder)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return session
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        ts = record.get("ts") if isinstance(record.get("ts"), (int, float)) else None
        if session.first_ts is None and ts is not None:
            session.first_ts = ts
        if record.get("kind") == "session_metadata":
            session.metadata = session.metadata or record
            session.has_agent = session.has_agent or isinstance(record.get("agent"), str)
            continue
        message = record.get("message") if record.get("kind") == "message" else (
            record if record.get("kind") is None else None)
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user" and session.first_user is None:
            session.first_user = str(message.get("content") or "").strip()
        for call in message.get("tool_calls") or ():
            if isinstance(call, dict):
                for text in _task_texts((call.get("function") or {}).get("arguments")):
                    session.tasks.append((ts or 0.0, text))
    return session


def _parents(sessions: list[_OldSession]) -> dict[Path, _OldSession]:
    """Each old subagent run's parent: a session without metadata whose first
    message is the text of a task call, in the session that made the call
    last before it started."""
    calls: dict[str, list[tuple[float, _OldSession]]] = {}
    for session in sessions:
        for ts, text in session.tasks:
            calls.setdefault(text, []).append((ts, session))
    parents: dict[Path, _OldSession] = {}
    for session in sessions:
        if session.metadata is not None or not session.first_user:
            continue
        candidates = [(ts, parent) for ts, parent in calls.get(session.first_user, ()) if parent is not session]
        if not candidates:
            continue
        started = session.first_ts if session.first_ts is not None else float("inf")
        before = [item for item in candidates if item[0] <= started]
        parents[session.path] = max(before or candidates, key=lambda item: item[0])[1]
    return parents


def _targets(sessions: list[_OldSession], parents: dict[Path, _OldSession]) -> dict[Path, Path]:
    targets: dict[Path, Path] = {}
    home_folder = session_store.folder_for(paths.user_home())

    def target(session: _OldSession, depth: int = 0) -> Path:
        if session.path in targets:
            return targets[session.path]
        parent = parents.get(session.path)
        cwd = session.metadata.get("cwd") if session.metadata else None
        if isinstance(cwd, str) and cwd:
            where = session_store.folder_for(cwd) / session.relative
        elif parent is not None and depth < len(sessions):
            where = session_store.subagent_folder(target(parent, depth + 1)) / session.relative
        else:
            where = home_folder / session.relative
        targets[session.path] = where
        return where

    for session in sessions:
        target(session)
    return targets


def _liveness_files(path: Path) -> tuple[Path, Path]:
    stem = f".{path.name}.liveness"
    return path.parent / f"{stem}.json", path.parent / f"{stem}.lock"


def _in_use(path: Path) -> bool:
    state, _ = _liveness_files(path)
    return state.exists() and session_catalog.session_in_flight(path)


def _old_session_folders(apply: bool) -> list[Path]:
    roots = [paths.sessions_root()]
    if not apply:
        roots.insert(0, paths.legacy_homes()["data"] / "sessions")
    folders = []
    for root in roots:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        folders.extend(entry for entry in entries
                       if not session_store.is_folder_name(entry.name) and not entry.name.startswith(".")
                       and entry.is_dir() and not entry.is_symlink())
    return folders


def _remove_empty_dirs(folder: Path) -> None:
    for directory, _dirnames, _filenames in os.walk(folder, topdown=False):
        with contextlib.suppress(OSError):
            os.rmdir(directory)


def _file_latest(folder: Path, targets: dict[Path, Path]) -> None:
    """Point the agent's latest.json in state/ at where its latest session went.
    The recorded path may predate the move into ~/.js; its tail under the
    agent's folder names the session."""
    latest = folder / "latest.json"
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    old = payload.get("session_file") if isinstance(payload, dict) else None
    old_parts = Path(old).parts if isinstance(old, str) else ()
    moved = next((new for path, new in targets.items()
                  if path.is_relative_to(folder) and new.is_file()
                  and old_parts[-len(path.relative_to(folder).parts) - 1:]
                  == (folder.name, *path.relative_to(folder).parts)), None)
    if moved is not None:
        session_store.write_latest(paths.state_root() / folder.name, moved)
    with contextlib.suppress(OSError):
        latest.unlink()


def refile_sessions(*, apply: bool) -> Iterator[Step]:
    """File the sessions of every old per-agent folder by the directory they
    started in. A dry run looks where the sessions are before the moves."""
    folders = _old_session_folders(apply)
    if not folders:
        return
    sessions = [_read_old_session(folder, path) for folder in folders
                for path in sorted(folder.rglob(f"*{session_store.SUFFIX}"))
                if path.is_file() and not path.is_symlink()]
    parents = _parents(sessions)
    targets = _targets(sessions, parents)
    walk = _Walk(apply=apply)
    for folder in folders:
        filed = 0
        for session in (item for item in sessions if item.folder == folder):
            if _in_use(session.path):
                yield Step("refuse", session.path, None, msgs.HOME_SESSION_IN_USE.text())
                continue
            target = targets[session.path]
            outcome = list(walk.entry(session.path, target))
            yield from (step for step in outcome if step.kind not in ("move", "relink"))
            if not any(step.kind == "move" for step in outcome):
                continue
            filed += 1
            for companion in sorted(session.path.parent.glob(f"{session.path.name}.bak*")):
                yield from (step for step in walk.entry(companion, target.with_name(companion.name))
                            if step.kind not in ("move", "relink"))
            if not apply:
                continue
            for sidecar in _liveness_files(session.path):
                with contextlib.suppress(OSError):
                    sidecar.unlink()
            session_store.link_file(target)
            if not session.has_agent:
                parent = parents.get(session.path)
                cwd = session.metadata.get("cwd") if session.metadata else None
                session_catalog.record_session_start(
                    target, cwd=cwd or paths.user_home(), agent=folder.name,
                    mode="subagent" if parent is not None else None,
                    parent=targets[parent.path] if parent is not None else None,
                    ts=session.first_ts if session.first_ts is not None else target.stat().st_mtime)
            else:
                session_text.refresh(target)
        if filed:
            yield Step("refile", folder, None, msgs.plural(filed, "session"))
        history = folder / ".history"
        if history.is_file():
            yield from walk.entry(history, paths.state_root() / folder.name / "history")
        if apply:
            _file_latest(folder, targets)
            _remove_empty_dirs(folder)
            if folder.exists():
                yield Step("skip", folder, None, msgs.HOME_NOT_EMPTY.text())


def steps(*, apply: bool) -> Iterator[Step]:
    """Every step of the migration, each performed as it is yielded when `apply` is true."""
    legacy = paths.legacy_homes()
    walk = _Walk(apply=apply)
    yield from walk.entry(legacy["inbox"], paths.work_dir())
    yield from walk.spread(legacy["config"], _CONFIG_ENTRIES)
    yield from walk.spread(legacy["data"], _DATA_ENTRIES)
    yield from convert_agents(apply=apply)
    yield from refile_sessions(apply=apply)


@contextlib.contextmanager
def _locked_home() -> Iterator[None]:
    home = paths.home()
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def migrate_once(out: TextIO | None = None) -> list[Step]:
    """The startup migration: runs when an old location exists and the marker
    does not, then writes the marker. With nothing to move it touches nothing.
    Each step is printed as it is done."""
    marker = paths.home_migration_marker()
    if marker.exists() or not any(os.path.lexists(path) for path in paths.legacy_homes().values()):
        return []
    stream = out if out is not None else sys.stderr
    done: list[Step] = []
    try:
        with _locked_home():
            if marker.exists():
                return []
            for step in steps(apply=True):
                done.append(step)
                msgs.say_said(describe(step, apply=True), file=stream)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n", encoding="utf-8")
    except OSError as exc:
        msgs.say(msgs.HOME_MIGRATION_FAILED, file=stream, home=_short(paths.home()), error=exc)
    return done


def sweep_tmp(now: float | None = None) -> list[Path]:
    """Remove top-level `~/.js/tmp` entries not modified for TMP_MAX_AGE_SECONDS."""
    root = paths.home() / "tmp"
    cutoff = (time.time() if now is None else now) - TMP_MAX_AGE_SECONDS
    removed: list[Path] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return removed
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
            if info.st_mtime >= cutoff:
                continue
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except OSError:
            continue
        removed.append(Path(entry.path))
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = msgs.ArgumentParser(prog="js.home", description=msgs.HOME_DESCRIPTION.text())
    parser.add_argument("--apply", action="store_true", help=msgs.OPT_HOME_APPLY.text())
    args = parser.parse_args(argv)
    found: list[Step] = []
    with _locked_home() if args.apply else contextlib.nullcontext():
        for step in steps(apply=args.apply):
            found.append(step)
            msgs.say_said(describe(step, apply=args.apply), flush=True)
    if not found:
        msgs.say(msgs.HOME_NOTHING_TO_MOVE, home=_short(paths.home()))
    return 1 if any(step.kind == "refuse" for step in found) else 0


if __name__ == "__main__":
    raise SystemExit(main())
