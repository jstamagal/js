"""Move the pre-`~/.js` locations into `~/.js`, and sweep `~/.js/tmp`.

    uv run python -m js.home [--apply]

Without --apply it prints what it would move. `migrate_once()` runs the same
move at js startup, once: the marker `paths.home_migration_marker()` records
that it ran, so later starts do nothing even if an old location reappears.

Every move is a rename of one entry, so a directory lands whole or not at
all. A symlink is moved as the link; the walk never descends through one.
When the destination already exists: an identical file or link drops the
source copy, a directory is merged entry by entry, and anything else is
refused with the reason and left where it was. Across filesystems an entry
is copied beside its destination, renamed into place, and only then removed
from the source.
"""

from __future__ import annotations

import argparse
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


def describe(step: Step, *, apply: bool) -> str:
    source, target = _short(step.source), _short(step.target) if step.target else ""
    if step.kind == "move":
        return f"{'moved' if apply else 'would move'} {source} -> {target}"
    if step.kind == "duplicate":
        verb = "removed" if apply else "would remove"
        return f"{verb} {source}: identical to {target}"
    if step.kind == "rmdir":
        return f"{'removed' if apply else 'would remove'} empty {source}"
    return f"{'refused' if apply else 'would refuse'} {source}: {step.reason}"


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
        return "a symlink"
    if stat.S_ISDIR(mode):
        return "a directory"
    return "a file"


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
    if stat.S_ISDIR(mode):
        shutil.rmtree(source)
    else:
        os.unlink(source)


def _rename(source: Path, target: Path, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _move_across(source, target, mode)


def _entry(source: Path, target: Path, *, apply: bool) -> Iterator[Step]:
    """Move one entry to `target`, merging into an existing directory there."""
    source_stat = _lstat(source)
    if source_stat is None:
        return
    target_stat = _lstat(target)
    if target_stat is None:
        if apply:
            try:
                _rename(source, target, source_stat.st_mode)
            except OSError as exc:
                yield Step("refuse", source, target, f"could not move to {_short(target)}: {exc}")
                return
        yield Step("move", source, target)
        return
    source_mode, target_mode = source_stat.st_mode, target_stat.st_mode
    if stat.S_ISDIR(source_mode) and stat.S_ISDIR(target_mode):
        try:
            children = sorted(os.listdir(source))
        except OSError as exc:
            yield Step("refuse", source, target, f"could not list it: {exc}")
            return
        for child in children:
            yield from _entry(source / child, target / child, apply=apply)
        yield from _remove_if_empty(source, apply=apply)
        return
    if _same(source, target, source_mode, target_mode):
        if apply:
            try:
                os.unlink(source)
            except OSError as exc:
                yield Step("refuse", source, target, f"identical to {_short(target)} but could not remove it: {exc}")
                return
        yield Step("duplicate", source, target)
        return
    if stat.S_ISREG(source_mode) and stat.S_ISREG(target_mode):
        reason = f"{_short(target)} already exists with different content"
    else:
        reason = f"{_short(target)} already exists as {_kind(target_mode)}; this is {_kind(source_mode)}"
    yield Step("refuse", source, target, reason)


def _remove_if_empty(directory: Path, *, apply: bool) -> Iterator[Step]:
    if not apply:
        return
    try:
        os.rmdir(directory)
    except OSError:
        return
    yield Step("rmdir", directory)


def _spread(root: Path, renames: dict, *, apply: bool) -> Iterator[Step]:
    """Move each entry of an old directory to ~/.js/<name> or its renamed place."""
    root_stat = _lstat(root)
    if root_stat is None:
        return
    if not stat.S_ISDIR(root_stat.st_mode):
        yield Step("refuse", root, None, f"is {_kind(root_stat.st_mode)}, not a directory; move it by hand")
        return
    try:
        # Entries that keep their name go first, so a directory another entry
        # is renamed into (state/, logs/) arrives whole before it is added to.
        names = sorted(os.listdir(root), key=lambda name: (name in renames, name))
    except OSError as exc:
        yield Step("refuse", root, None, f"could not list it: {exc}")
        return
    for name in names:
        target = renames[name]() if name in renames else paths.home() / name
        yield from _entry(root / name, target, apply=apply)
    yield from _remove_if_empty(root, apply=apply)


def plan_or_apply(*, apply: bool) -> list[Step]:
    """Every step of the migration, performed when `apply` is true."""
    legacy = paths.legacy_homes()
    steps: list[Step] = []
    steps.extend(_entry(legacy["inbox"], paths.work_dir(), apply=apply))
    steps.extend(_spread(legacy["config"], _CONFIG_RENAMES, apply=apply))
    steps.extend(_spread(legacy["data"], _DATA_RENAMES, apply=apply))
    return steps


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
    does not, then writes the marker. With nothing to move it touches nothing."""
    marker = paths.home_migration_marker()
    if marker.exists() or not any(os.path.lexists(path) for path in paths.legacy_homes().values()):
        return []
    stream = out if out is not None else sys.stderr
    try:
        with _locked_home():
            if marker.exists():
                return []
            steps = plan_or_apply(apply=True)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"js: could not migrate to {_short(paths.home())}: {exc}", file=stream)
        return []
    for step in steps:
        print(f"js: {describe(step, apply=True)}", file=stream)
    return steps


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
    parser = argparse.ArgumentParser(prog="js.home", description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="move; default is a dry run")
    args = parser.parse_args(argv)
    if args.apply:
        with _locked_home():
            steps = plan_or_apply(apply=True)
    else:
        steps = plan_or_apply(apply=False)
    for step in steps:
        print(describe(step, apply=args.apply))
    if not steps:
        print(f"nothing to move into {_short(paths.home())}")
    return 1 if any(step.kind == "refuse" for step in steps) else 0


if __name__ == "__main__":
    raise SystemExit(main())
