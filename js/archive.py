"""Rotated files: the archive tree that mirrors the home, and the one place
that decides whether a file is read from its live path or from its archive.

A rotated file lives at `paths.archive_root()` / its path relative to the
home, with the compression suffix added:

    ~/.js/sessions/-home-me-proj/2026-10-01T1200-ab12.jsonl
      -> ~/.js/archive/sessions/-home-me-proj/2026-10-01T1200-ab12.jsonl.xz
    ~/.js/logs/defaultagent/2026-10-01T1200-ab12.log
      -> ~/.js/archive/logs/defaultagent/2026-10-01T1200-ab12.log.xz

One archive file holds one original file, byte for byte. Everything outside
this module names a file by its live path; `located` says where its bytes
are, `open_read` reads them, `restore` puts them back at the live path.

A file can exist at both places only in these states, and in each one the
live file holds every byte of the archive as its prefix:

- a rotation stopped after the archive was written and before the live file
  was removed (the two are identical);
- a restore stopped after the live file was linked and before the archive
  was removed, and the live file may have been appended to since.

So the live file wins whenever it exists, and `restore` never removes an
archive beside one. An archive that is not a prefix of its live file is a
conflict `archive_file` refuses: it leaves both files alone.
"""

from __future__ import annotations

import contextlib
import fcntl
import gzip
import hashlib
import lzma
import os
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from . import paths

_CHUNK = 1 << 20
_XZ_PRESET = 6
_GZIP_LEVEL = 6
# Compression format name -> the suffix its archive files carry.
FORMATS = {"xz": ".xz", "gz": ".gz"}


def _opener(suffix: str):
    return lzma.open if suffix == ".xz" else gzip.open


def _compressor(fmt: str):
    if fmt == "xz":
        return lzma.LZMACompressor(preset=_XZ_PRESET)
    if fmt == "gz":
        import zlib

        return zlib.compressobj(_GZIP_LEVEL, zlib.DEFLATED, 31)
    raise ValueError(f"unknown compression format: {fmt}")


def archive_base(live: Path) -> Path | None:
    """Where `live`'s archive goes, without the compression suffix, or None
    when `live` is outside the home."""
    try:
        relative = Path(os.path.abspath(live)).relative_to(Path(os.path.abspath(paths.home())))
    except ValueError:
        return None
    if relative.parts and relative.parts[0] == paths.archive_root().name:
        return None
    return paths.archive_root() / relative


def archive_path(live: Path, fmt: str) -> Path | None:
    """The archive file `live` rotates to in format `fmt`."""
    base = archive_base(live)
    return None if base is None else base.with_name(base.name + FORMATS[fmt])


def archives_of(live: Path) -> list[Path]:
    """Every archive of `live` that exists, in `FORMATS` order."""
    base = archive_base(live)
    if base is None:
        return []
    return [candidate for suffix in FORMATS.values()
            if (candidate := base.with_name(base.name + suffix)).is_file()]


def live_path(archived: Path) -> Path | None:
    """The live path an archive file under `paths.archive_root()` stands for."""
    try:
        relative = Path(archived).relative_to(paths.archive_root())
    except ValueError:
        return None
    if relative.suffix not in FORMATS.values():
        return None
    return paths.home() / relative.with_suffix("")


def located(live: Path) -> Path | None:
    """The file that holds `live`'s bytes: `live` itself when it exists, else
    its archive, else None."""
    live = Path(live)
    if live.is_file():
        return live
    found = archives_of(live)
    return found[0] if found else None


def exists(live: Path) -> bool:
    """Whether `live` is a file, live or rotated."""
    return located(live) is not None


def is_archived(live: Path) -> bool:
    """Whether `live`'s bytes are only in the archive."""
    found = located(live)
    return found is not None and found != Path(live)


def stat(live: Path) -> os.stat_result:
    """The stat of the file that holds `live`'s bytes. Its mtime is the
    original's: rotation keeps it."""
    found = located(live)
    if found is None:
        raise FileNotFoundError(2, "No such file", str(live))
    return found.stat()


@contextlib.contextmanager
def open_read(live: Path, *, binary: bool = False, shared_lock: bool = False) -> Iterator[IO[Any]]:
    """Open `live`'s bytes for reading, from the live file or its archive.
    `shared_lock` takes LOCK_SH on a live file, as session readers do; an
    archive is never written, so it needs none."""
    found = located(live)
    if found is None:
        raise FileNotFoundError(2, "No such file", str(live))
    if found == Path(live):
        kwargs = {} if binary else {"encoding": "utf-8", "errors": "replace"}
        with open(found, "rb" if binary else "r", **kwargs) as stream:
            if shared_lock:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            yield stream
        return
    opener = _opener(found.suffix)
    if binary:
        with opener(found, "rb") as stream:
            yield stream
    else:
        with opener(found, "rt", encoding="utf-8", errors="replace") as stream:
            yield stream


def files_under(live_root: Path, pattern: str) -> list[Path]:
    """The live paths of every file under `live_root` matching the glob
    `pattern`, rglob'd, live or rotated, each once, sorted. Hidden names are
    left out."""
    live_root = Path(live_root)
    found: set[Path] = set()
    with contextlib.suppress(OSError):
        found.update(path for path in live_root.rglob(pattern)
                     if not path.name.startswith(".") and path.is_file())
    base = archive_base(live_root)
    if base is not None and base.is_dir():
        for suffix in FORMATS.values():
            with contextlib.suppress(OSError):
                for archived in base.rglob(pattern + suffix):
                    if archived.name.startswith(".") or not archived.is_file():
                        continue
                    found.add(live_root / archived.relative_to(base).with_suffix(""))
    return sorted(found)


def files_in(live_dir: Path, pattern: str) -> list[Path]:
    """`files_under` for one directory, not its subdirectories."""
    live_dir = Path(live_dir)
    found: set[Path] = set()
    with contextlib.suppress(OSError):
        found.update(path for path in live_dir.glob(pattern)
                     if not path.name.startswith(".") and path.is_file())
    base = archive_base(live_dir)
    if base is not None and base.is_dir():
        for suffix in FORMATS.values():
            with contextlib.suppress(OSError):
                found.update(live_dir / archived.with_suffix("").name
                             for archived in base.glob(pattern + suffix)
                             if not archived.name.startswith(".") and archived.is_file())
    return sorted(found)


# --- moving bytes ------------------------------------------------------------


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _staging(beside: Path) -> Path:
    return beside.with_name(f".{beside.name}.{secrets.token_hex(4)}.tmp")


def _digest_archive(archived: Path, suffix: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _opener(suffix)(archived, "rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def is_prefix(archived: Path, live: Path) -> bool:
    """Whether the bytes `archived` holds are a prefix of `live`'s."""
    try:
        with _opener(archived.suffix)(archived, "rb") as packed, open(live, "rb") as plain:
            while chunk := packed.read(_CHUNK):
                if plain.read(len(chunk)) != chunk:
                    return False
    except (OSError, EOFError, lzma.LZMAError):
        return False
    return True


def _prune(directory: Path) -> None:
    """Remove `directory` and its parents while they are empty, up to the archive root."""
    root = paths.archive_root()
    while directory != root and directory.is_relative_to(root):
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


@dataclass(frozen=True)
class Archived:
    """One rotation: the live file, its archive, and the bytes of each."""

    live: Path
    archive: Path
    before: int
    after: int


class Changed(Exception):
    """The file changed while it was being archived; it is left live."""


class InUse(Exception):
    """A process opened the file while it was being archived; it is left live."""


class Conflict(Exception):
    """An archive exists that is not a prefix of the live file; both are left alone."""


def _make_dirs(directory: Path) -> None:
    """`directory` and its missing ancestors, each new one fsynced into its parent."""
    missing: list[Path] = []
    while not directory.is_dir():
        missing.append(directory)
        directory = directory.parent
    for each in reversed(missing):
        with contextlib.suppress(FileExistsError):
            each.mkdir()
        _fsync_dir(each.parent)


def _settle(out: IO[bytes], info: os.stat_result) -> None:
    """Give the file open as `out` the mode and times of `info`, then fsync it."""
    out.flush()
    os.fchmod(out.fileno(), info.st_mode & 0o7777)
    os.utime(out.fileno(), ns=(info.st_atime_ns, info.st_mtime_ns))
    os.fsync(out.fileno())


def archive_file(live: Path, fmt: str, *, source: IO[bytes] | None = None,
                 unused: Callable[[], bool] | None = None) -> Archived:
    """Compress `live` into its archive and remove it.

    The archive is written beside its final name, checked to decompress to
    exactly `live`'s bytes, given `live`'s mode and times, fsynced and renamed
    into place; only then, and only if `live`'s size and mtime are what they
    were when the copy started and `unused()` (when given) still says no
    process has it open, is `live` removed. A stop at any point leaves `live`,
    or a complete checked archive, or both identical. `source` is `live`
    already open for reading (a caller holding its lock)."""
    live = Path(live)
    target = archive_path(live, fmt)
    if target is None:
        raise ValueError(f"not under the home: {live}")
    for other in archives_of(live):
        if not is_prefix(other, live):
            raise Conflict(str(other))
    _make_dirs(target.parent)
    with contextlib.ExitStack() as stack:
        stream = source if source is not None else stack.enter_context(open(live, "rb"))
        stream.seek(0)
        info = os.fstat(stream.fileno())
        staging = _staging(target)
        digest = hashlib.sha256()
        size = 0
        try:
            compressor = _compressor(fmt)
            with open(staging, "wb") as out:
                while chunk := stream.read(_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(compressor.compress(chunk))
                out.write(compressor.flush())
                _settle(out, info)
            if _digest_archive(staging, FORMATS[fmt]) != (digest.hexdigest(), size):
                raise OSError(f"archive does not decompress to the original: {live}")
            now = os.stat(live)
            if (now.st_size, now.st_mtime_ns, now.st_ino) != (info.st_size, info.st_mtime_ns, info.st_ino):
                raise Changed(str(live))
            os.replace(staging, target)
        except BaseException:
            with contextlib.suppress(OSError):
                staging.unlink()
            raise
        _fsync_dir(target.parent)
        # From here to the unlink, live and archive are identical; a
        # restore leaves both alone.
        now = os.stat(live)
        if (now.st_size, now.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
            raise Changed(str(live))
        if unused is not None and not unused():
            raise InUse(str(live))
        for other in archives_of(live):
            if other != target:
                other.unlink()
        live.unlink()
        _fsync_dir(live.parent)
    return Archived(live, target, size, target.stat().st_size)


def restore(live: Path) -> bool:
    """Put `live`'s bytes back at `live` from its archive. True when it did.

    A live file that already exists stays as it is, and so does its archive:
    the live file holds every byte the archive does, and the next rotation
    of it replaces the archive. The restored copy is written beside `live`,
    given the archive's mode and times, fsynced and linked into place; only
    then is the archive removed. A failure (a full disk) removes the partial
    copy and leaves the archive."""
    live = Path(live)
    found = archives_of(live)
    if not found or live.exists():
        return False
    archived = found[0]
    info = archived.stat()
    _make_dirs(live.parent)
    staging = _staging(live)
    try:
        with _opener(archived.suffix)(archived, "rb") as packed, open(staging, "wb") as out:
            while chunk := packed.read(_CHUNK):
                out.write(chunk)
            _settle(out, info)
        try:
            os.link(staging, live)
        except FileExistsError:
            # Another process restored it first.
            return False
        finally:
            staging.unlink()
    except BaseException:
        with contextlib.suppress(OSError):
            staging.unlink()
        raise
    _fsync_dir(live.parent)
    for other in found:
        other.unlink()
    _fsync_dir(archived.parent)
    _prune(archived.parent)
    return True
