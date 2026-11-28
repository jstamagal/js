"""`js init` in a js home: the stock setup, `defaults/` whole, laid into the
home at the same relative paths, as `/etc/skel` is laid into a new user's
home, with every directory of the layout (`paths.layout_dirs`).

A copy in the home replaces the shipped file of the same relative path, so
the home's copies are read instead of the shipped ones until `js init -f`
refreshes them.

Three forms:
- plain: copies what is missing and replaces nothing, like
  `rsync -a --ignore-existing`; it then names the home's files that differ
  from the stock ones.
- `force`: like `cp -a`: a stock file replaces the home's file of the same
  relative path when their bytes differ. A file the stock setup does not
  have is left alone; nothing is deleted.
- `backup`: renames the home to `<home>-YYYY-MM-DD` (`-2`, `-3`, ... for a
  second the same day), then makes a new home from the stock setup alone.
  Sessions, logs, logins and the rest stay in the renamed home.
- `archive`: `backup`, then `<home>-YYYY-MM-DD.tar.gz` of the renamed home
  beside it, and the renamed home removed, so the old home is the one
  tarball. The tarball is written under a temporary name, fsynced, read
  back whole and compared byte for byte with the renamed home (`verify`),
  and linked into place before anything is removed; then only what it was
  checked to hold, unchanged since, is removed.
  A tarball that fails any of that is removed and the renamed home is left
  whole.

A symlink at a stock path, and a file where the stock setup has a directory
or the other way round, are left alone in every form. No form runs git.
"""

from __future__ import annotations

import contextlib
import datetime
import filecmp
import gzip
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tarfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import messages as msgs, paths, session_catalog, session_store


@dataclass
class Laid:
    """What laying the stock setup into a home did, by path relative to it."""

    made_files: list[str] = field(default_factory=list)
    made_dirs: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


def _stock() -> tuple[list[str], list[str]]:
    """The stock setup's directories and files, relative to `defaults/`, in
    walk order: a directory before what it holds."""
    root = paths.defaults_dir()
    dirs: list[str] = []
    files: list[str] = []
    for top, subdirs, names in os.walk(root):
        subdirs.sort()
        rel = Path(top).relative_to(root)
        if rel != Path("."):
            dirs.append(str(rel))
        files.extend(str(rel / name) for name in sorted(names))
    return dirs, files


def _copy_new(source: Path, target: Path) -> bool:
    """Copy ``source`` to ``target`` with its mode and times; False when
    something is already at ``target``."""
    try:
        with open(source, "rb") as src, open(target, "xb") as dst:
            shutil.copyfileobj(src, dst)
    except FileExistsError:
        return False
    shutil.copystat(source, target)
    return True


def _replace(source: Path, target: Path) -> None:
    """``target`` becomes a copy of ``source``, renamed into place."""
    staged = target.with_name(f".{target.name}.js-init")
    shutil.copy2(source, staged)
    os.replace(staged, target)


def _under_kept(rel: str, kept_dirs: set[str]) -> bool:
    return any(rel == kept or rel.startswith(kept + os.sep) for kept in kept_dirs)


def lay(home: Path, *, force: bool = False) -> Laid:
    """Lay the stock setup and the layout directories into ``home``."""
    stock = paths.defaults_dir()
    laid = Laid()
    blocked: set[str] = set()
    stock_dirs, stock_files = _stock()
    for rel in stock_dirs:
        if _under_kept(rel, blocked):
            continue
        target = home / rel
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            laid.kept.append(rel + "/")
            blocked.add(rel)
            continue
        with contextlib.suppress(FileExistsError):
            target.mkdir()
            laid.made_dirs.append(rel + "/")
    for rel in stock_files:
        if _under_kept(rel, blocked):
            continue
        source, target = stock / rel, home / rel
        if _copy_new(source, target):
            laid.made_files.append(rel)
        elif force and target.is_file() and not target.is_symlink() and not filecmp.cmp(source, target, shallow=False):
            _replace(source, target)
            laid.replaced.append(rel)
        else:
            laid.kept.append(rel)
    for directory in paths.layout_dirs():
        if not directory.is_relative_to(home):
            continue
        rel = str(directory.relative_to(home)) + "/"
        if rel in laid.made_dirs or rel in laid.kept:
            continue
        try:
            directory.mkdir(parents=True)
        except FileExistsError:
            continue
        laid.made_dirs.append(rel)
    return laid


def differing(home: Path) -> list[str]:
    """The home's files at a stock path whose bytes differ from the stock
    file's: what `js init -f` would replace."""
    stock = paths.defaults_dir()
    found = []
    for rel in _stock()[1]:
        target = home / rel
        if target.is_file() and not target.is_symlink() and not filecmp.cmp(stock / rel, target, shallow=False):
            found.append(rel)
    return found


def backup_name(home: Path, today: datetime.date | None = None) -> Path:
    """`<home>-YYYY-MM-DD`, or with `-2`, `-3`, ... the first that is free,
    its `.tar.gz` name included."""
    stamp = (today or datetime.date.today()).isoformat()
    candidate = home.with_name(f"{home.name}-{stamp}")
    number = 1
    while any(path.exists() or path.is_symlink() for path in (candidate, tarball_name(candidate))):
        number += 1
        candidate = home.with_name(f"{home.name}-{stamp}-{number}")
    return candidate


def tarball_name(moved: Path) -> Path:
    return moved.with_name(moved.name + ".tar.gz")


class Incomplete(OSError):
    """A tarball that does not hold what it was written from."""


# One entry of a home as a tarball must hold it: (kind, size or link target).
Entry = tuple[str, int | str]
_CHUNK = 1 << 20
_HEX = re.compile(r"[0-9a-f]{64}")


class HasherFailed(Exception):
    """The hasher program failed or printed no digest."""


class _Sha256:
    name = "sha256"

    def __init__(self) -> None:
        self._digest = hashlib.sha256()

    def update(self, data: bytes) -> None:
        self._digest.update(data)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()

    def close(self) -> None:
        pass


class _B3sum:
    """One `b3sum` process hashing everything written to it as one stream."""

    name = "BLAKE3"

    def __init__(self, program: str) -> None:
        try:
            self._process = subprocess.Popen([program], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE)
        except OSError as exc:
            raise HasherFailed(str(exc)) from exc

    def update(self, data: bytes) -> None:
        try:
            self._process.stdin.write(data)
        except OSError as exc:
            self._process.kill()
            self._process.wait()
            raise HasherFailed(str(exc)) from exc

    def hexdigest(self) -> str:
        try:
            out, err = self._process.communicate()
        except OSError as exc:
            raise HasherFailed(str(exc)) from exc
        words = out.decode("ascii", "replace").split()
        if self._process.returncode != 0 or not words or not _HEX.fullmatch(words[0]):
            detail = err.decode("utf-8", "replace").strip() or f"exit {self._process.returncode}"
            raise HasherFailed(detail)
        return words[0]

    def close(self) -> None:
        """Stop the process if it is still running."""
        if self._process.poll() is None:
            self._process.kill()
            self._process.communicate()


def hashers() -> list[Callable[[], _Sha256 | _B3sum]]:
    """The hashers to try in turn: `b3sum` when it is on PATH, then sha256."""
    program = shutil.which("b3sum")
    found: list[Callable[[], _Sha256 | _B3sum]] = []
    if program is not None:
        found.append(lambda: _B3sum(program))
    found.append(_Sha256)
    return found


def _member(moved: Path, path: Path) -> str:
    return str(Path(moved.name, path.relative_to(moved)))


def _walk(moved: Path) -> dict[str, Path]:
    """Every path in ``moved``, by tarball member name, ``moved`` included."""
    found = {moved.name: moved}
    for top, dirs, files in os.walk(moved):
        for name in (*dirs, *files):
            path = Path(top, name)
            found[_member(moved, path)] = path
    return found


def _stamp(path: Path) -> tuple[int, int, int]:
    info = path.lstat()
    return info.st_mode, info.st_size, info.st_mtime_ns


def _on_disk(path: Path) -> Entry | None:
    """``path`` as a tarball entry; None for a kind tar does not archive."""
    if path.is_symlink():
        return ("link", os.readlink(path))
    if path.is_dir():
        return ("dir", 0)
    if path.is_file():
        return ("file", path.lstat().st_size)
    if stat.S_ISFIFO(path.lstat().st_mode):
        return ("fifo", 0)
    return None


def _feed(stream, hasher) -> None:
    while chunk := stream.read(_CHUNK):
        hasher.update(chunk)


def _read_tarball(tarball: Path, by_member: dict[str, Path], hasher) -> tuple[list[str], set[str]]:
    """Read ``tarball`` from start to end: each member's kind, size and link
    target checked against the path it was written from, and every regular
    file's bytes, in member order, fed to ``hasher``. The regular files in
    that order, and every member."""
    files: list[str] = []
    seen: set[str] = set()
    try:
        with gzip.open(tarball, "rb") as stream:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for member in tar:
                    path = by_member.get(member.name)
                    if path is None:
                        raise Incomplete(msgs.INIT_HOME_ARCHIVE_EXTRA.text(member=member.name))
                    if member.issym():
                        held: Entry = ("link", member.linkname)
                    elif member.isdir():
                        held = ("dir", 0)
                    elif member.isfifo():
                        held = ("fifo", 0)
                    elif member.islnk():
                        # A hard link has no body of its own: it is the file
                        # it names, whose bytes are already in the stream.
                        linked = by_member.get(member.linkname)
                        if linked is None or not os.path.samefile(linked, path):
                            raise Incomplete(msgs.INIT_HOME_ARCHIVE_DIFFERS.text(member=member.name))
                        held = ("file", path.lstat().st_size)
                    elif member.isfile():
                        held = ("file", member.size)
                        _feed(tar.extractfile(member), hasher)
                        files.append(member.name)
                    else:
                        raise Incomplete(msgs.INIT_HOME_ARCHIVE_DIFFERS.text(member=member.name))
                    if _on_disk(path) != held:
                        raise Incomplete(msgs.INIT_HOME_ARCHIVE_DIFFERS.text(member=member.name))
                    seen.add(member.name)
            while stream.read(_CHUNK):
                pass
    except (EOFError, tarfile.TarError, gzip.BadGzipFile, zlib.error) as exc:
        raise Incomplete(msgs.INIT_HOME_ARCHIVE_UNREADABLE.text(error=exc)) from exc
    return files, seen


def _first_differing(tarball: Path, by_member: dict[str, Path]) -> str | None:
    """The first regular file whose bytes in ``tarball`` differ from the
    file's: one sha256 per file, for naming it."""
    with tarfile.open(tarball, "r:gz") as tar:
        for member in tar:
            if member.isfile():
                packed, here = hashlib.sha256(), hashlib.sha256()
                _feed(tar.extractfile(member), packed)
                with open(by_member[member.name], "rb") as stream:
                    _feed(stream, here)
                if packed.digest() != here.digest():
                    return member.name
    return None


def _compare(tarball: Path, moved: Path, make_hasher) -> tuple[str, dict[str, tuple[int, int, int]]]:
    by_member = _walk(moved)
    stamps = {name: _stamp(path) for name, path in by_member.items()}
    packed = make_hasher()
    try:
        files, seen = _read_tarball(tarball, by_member, packed)
        packed_digest = packed.hexdigest()
    finally:
        packed.close()
    here = make_hasher()
    try:
        for name in files:
            with open(by_member[name], "rb") as stream:
                _feed(stream, here)
        here_digest = here.hexdigest()
    finally:
        here.close()
    if here_digest != packed_digest:
        differs = _first_differing(tarball, by_member) or files[0]
        raise Incomplete(msgs.INIT_HOME_ARCHIVE_DIFFERS.text(member=differs))
    return packed.name, {name: stamps[name] for name in seen}


def verify(tarball: Path, moved: Path) -> tuple[str, dict[str, tuple[int, int, int]]]:
    """The equivalent of `gzip -t` and `tar --compare`: read ``tarball``
    from start to end and compare every member with the path in ``moved``
    it was written from, for kind, size, symlink target and bytes. Every
    path in ``moved`` is in the tarball and nothing else is. Raises
    Incomplete when any of that fails; else returns the name of the hash
    the bytes were compared with, and each path's mode, size and mtime as
    they were when checked, by member name.

    A listing of the members is not enough: tar's own checksums cover each
    header only, so a corrupt or short file body lists cleanly. Reading
    every body through gzip checks the CRC over the whole stream, and the
    bytes are compared as two streams, every regular file in member order,
    one from the tarball and one from ``moved``, each through one hasher:
    one `b3sum` process each when it is on PATH, else sha256. A `b3sum`
    that fails is reported and the comparison runs again with sha256."""
    by_member = _walk(moved)
    choices = hashers()
    for index, make_hasher in enumerate(choices):
        try:
            name, stamps = _compare(tarball, moved, make_hasher)
        except HasherFailed as exc:
            if index + 1 == len(choices):
                raise Incomplete(str(exc)) from exc
            msgs.warn(msgs.INIT_HOME_HASHER_FAILED, error=exc)
            continue
        missing = [member for member, path in by_member.items()
                   if member not in stamps and _on_disk(path) is not None]
        if missing:
            raise Incomplete(msgs.INIT_HOME_ARCHIVE_INCOMPLETE.text(count=len(missing), first=missing[0]))
        return name, stamps
    raise AssertionError("unreachable")


def _written(staged: Path) -> None:
    """Called with the tarball once it is written and fsynced, before it is
    read back."""


def write_tarball(moved: Path) -> tuple[Path, str, dict[str, tuple[int, int, int]]]:
    """`<moved>.tar.gz` holding the directory ``moved``, the hash its bytes
    were compared with, and the stamps of the paths it was checked to hold
    (`verify`). Written under a temporary
    name, fsynced, read back and checked, then linked into place; on any
    failure the temporary file is removed and nothing else is touched."""
    target = tarball_name(moved)
    staged = target.with_name(f".{target.name}.partial")
    try:
        with open(staged, "xb") as stream:
            with tarfile.open(fileobj=stream, mode="w:gz") as tar:
                tar.add(moved, arcname=moved.name)
            stream.flush()
            os.fsync(stream.fileno())
        _written(staged)
        hash_name, held = verify(staged, moved)
        os.link(staged, target)
        _sync_dir(target.parent)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    staged.unlink()
    return target, hash_name, held


def _sync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_archived(moved: Path, held: dict[str, tuple[int, int, int]]) -> list[Path]:
    """Remove from ``moved`` each file and symlink a checked tarball holds,
    when its mode, size and mtime are still those it was checked with, then
    each directory left empty. The paths left behind: anything else."""
    left: list[Path] = []
    for top, dirs, files in os.walk(moved, topdown=False):
        for name in files + [d for d in dirs if Path(top, d).is_symlink()]:
            path = Path(top, name)
            if held.get(_member(moved, path)) == _stamp(path):
                path.unlink()
            else:
                left.append(path)
        try:
            os.rmdir(top)
        except OSError:
            if not any(path.is_relative_to(top) for path in left):
                left.append(Path(top))
    return left


def _live_session(home: Path) -> Path | None:
    """A session under ``home`` whose liveness record names a live process."""
    for folder in session_store.folders():
        if not folder.is_relative_to(home):
            continue
        with contextlib.suppress(OSError):
            for session in folder.rglob(f"*{session_store.SUFFIX}"):
                if session.is_file() and session_catalog.session_in_flight(session) is True:
                    return session
    return None


def _say(home: Path, laid: Laid) -> None:
    if laid.made_files or laid.made_dirs:
        dirs = len(laid.made_dirs)
        msgs.say(msgs.INIT_HOME_MADE, files=msgs.plural(len(laid.made_files), "file"),
                 dirs=(msgs.COUNT_DIRECTORY if dirs == 1 else msgs.COUNT_DIRECTORIES).words(number=dirs), home=home,
                 kept=msgs.plural(len(laid.kept), "path"))
    else:
        msgs.say(msgs.INIT_HOME_NOTHING_MADE, home=home, kept=msgs.plural(len(laid.kept), "path"))


def run(home: Path, *, force: bool = False, backup: bool = False, archive: bool = False) -> int:
    """`js init`, `js init -f`, `js init -b` or `js init -a` in the home ``home``."""
    backup = backup or archive
    moved = None
    try:
        if backup:
            live = _live_session(home)
            if live is not None:
                msgs.warn(msgs.INIT_HOME_LIVE, session=live)
                return 1
            moved = backup_name(home)
            os.rename(home, moved)
            msgs.say(msgs.INIT_HOME_MOVED, home=home, moved=moved)
            home.mkdir()
        laid = lay(home, force=force)
    except OSError as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 1
    for rel in laid.replaced:
        msgs.say(msgs.INIT_HOME_REPLACED, path=home / rel)
    _say(home, laid)
    if not force and not backup:
        differ = differing(home)
        if differ:
            msgs.say(msgs.INIT_HOME_DIFFER, files=msgs.plural(len(differ), "file"))
            for rel in differ:
                msgs.say(msgs.INIT_HOME_DIFFER_PATH, path=home / rel)
    if archive and moved is not None:
        msgs.warn(msgs.INIT_HOME_ARCHIVING, path=tarball_name(moved))
        try:
            made, hash_name, held = write_tarball(moved)
        except OSError as exc:
            msgs.warn(msgs.INIT_HOME_ARCHIVE_FAILED, path=tarball_name(moved), error=exc, moved=moved)
            return 1
        msgs.say(msgs.INIT_HOME_ARCHIVED, path=made, hash=hash_name)
        try:
            left = remove_archived(moved, held)
        except OSError as exc:
            msgs.warn(msgs.INIT_HOME_REMOVE_FAILED, moved=moved, error=exc)
            return 1
        if left:
            msgs.warn(msgs.INIT_HOME_LEFT, moved=moved, paths=msgs.plural(len(left), "path"))
            return 1
    return 0
