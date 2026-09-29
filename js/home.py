"""Move the pre-`~/.js` locations into `~/.js`, and sweep `~/.js/tmp`.

    uv run python -m js.home [--apply]

Without --apply it prints what it would move. `migrate_once()` runs the same
move at js startup, once: the marker `paths.home_migration_marker()` records
that it ran, so later starts do nothing even if an old location reappears.

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
import os
import shutil
import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import messages as msgs
from . import paths

# Entries of the old data directory that do not keep their name under ~/.js.
# Every other entry of the old config and data directories lands at ~/.js/<name>.
_DATA_RENAMES = {
    "transcript": paths.transcript_root,
    "modelsdotdev": paths.model_catalog_dir,
    "notes": paths.notes_dir,
    "commit-backups": paths.commit_backups_dir,
}
_CONFIG_RENAMES = {
    "logins.toml": lambda: paths.login_store_dir() / "logins.toml",
    "models-cache.json": lambda: paths.login_store_dir() / "models-cache.json",
}

# `~/.js/tmp` entries untouched for this long are removed at startup.
TMP_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Step:
    kind: str  # "move", "duplicate", "refuse", "rmdir"
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
            return
        try:
            _rename(source, target, mode)
        except _OldCopyLeft as exc:
            yield Step("move", source, target)
            yield Step("refuse", source, target, msgs.HOME_OLD_COPY_LEFT.text(target=_short(target), error=exc))
            return
        except OSError as exc:
            yield Step("refuse", source, target, msgs.HOME_NOT_MOVED.text(target=_short(target), error=exc))
            return
        yield Step("move", source, target)

    def remove_if_empty(self, directory: Path) -> Iterator[Step]:
        if not self.apply:
            return
        try:
            os.rmdir(directory)
        except OSError:
            return
        yield Step("rmdir", directory)

    def spread(self, root: Path, renames: dict) -> Iterator[Step]:
        """Move each entry of an old directory to ~/.js/<name> or its renamed place."""
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
            # Entries that keep their name go first, so a directory another entry
            # is renamed into (state/, logs/) arrives whole before it is added to.
            names = sorted(os.listdir(root), key=lambda name: (name in renames, name))
        except OSError as exc:
            yield Step("refuse", root, None, msgs.HOME_NOT_LISTED.text(error=exc))
            return
        for name in names:
            target = renames[name]() if name in renames else paths.home() / name
            yield from self.entry(root / name, target)
        yield from self.remove_if_empty(root)


def steps(*, apply: bool) -> Iterator[Step]:
    """Every step of the migration, each performed as it is yielded when `apply` is true."""
    legacy = paths.legacy_homes()
    walk = _Walk(apply=apply)
    yield from walk.entry(legacy["inbox"], paths.work_dir())
    yield from walk.spread(legacy["config"], _CONFIG_RENAMES)
    yield from walk.spread(legacy["data"], _DATA_RENAMES)


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
