"""The rules of `js cleanup`, as code: no model call, the same answer every
run.

A workspace is a directory agents work in (`~/.js/work`, each harness
directory of `~/inbox/agents`). Each directory at its top is one piece of
work; each loose file at its top is gathered by the day it was last modified.
A piece of work moves through stages on a clock counted from its newest file
modification:

- live, in the workspace, untouched, until `CLEANUP_ARCHIVE_AGE_MAX`;
- archived: moved to the workspace's archive area, outside the workspace,
  with its junk gone. Text is kept as plain files. A build directory gives up
  its configuration, compiler identification, build information, compile
  commands, test log, the text an agent left in it and its finished programs
  and shared libraries, then goes. Every other binary goes. A linked git
  worktree is retired once its repository has its work, with a patch of what
  the repository lacks written first;
- gzipped at `CLEANUP_GZIP_AGE_MAX`: each file compressed in place, one `.gz`
  per file;
- tarred at `CLEANUP_TAR_AGE_MAX`: one `.tar` beside the index, for good.

Each stage change leaves the piece whole in one stage or the other when it is
interrupted, and the next run finishes or undoes it. The archive area holds
the index of what is archived (`INDEX.md`, never compressed), what each stage
did to each piece (`records/<piece>.json`) and the descriptions the cleanup
agent writes (`descriptions/<piece>.md`). The workspace's `INDEX.md` keeps a
section listing what is still live in it. Every deletion and move of every
run, whatever the workspace, is one line of `log_file()`.

Every path this module removes or moves is inside the workspace or its
archive area; `_inside` refuses anything else.
"""

from __future__ import annotations

import errno
import gzip
import json
import os
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paths as _paths
from .session_store import slug

INDEX = "INDEX.md"
RECORDS = "records"
DESCRIPTIONS = "descriptions"
SECTION_START = "<!-- cleanup index -->"
SECTION_END = "<!-- /cleanup index -->"
# Where a build directory's kept files go inside its piece of work.
BUILD_KEPT = "build-kept"
# An index entry lists at most this many files of a piece.
LISTED_FILES_MAX = 500

_DAY = 86400
# Directories a tool regenerates whole.
_JUNK_TREES = frozenset({
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".triton-cache", ".cache", "cache", ".venv", "venv",
})
# A directory holding one of these and none of `_SOURCE_MARKERS` is a build directory.
_BUILD_MARKERS = ("CMakeCache.txt", "CMakeFiles", "build.ninja", "config.status", "meson-info")
_SOURCE_MARKERS = ("CMakeLists.txt", "configure", "configure.ac", "meson.build", ".git")
# Text a build writes at its top that says how it was configured.
_BUILD_TOP_KEPT = frozenset({"CMakeCache.txt", "compile_commands.json", "config.log"})
# Text an agent leaves in a build directory by running from it, by suffix,
# and the text with those suffixes a build writes itself.
_AGENT_TEXT_SUFFIXES = frozenset({".log", ".csv", ".tsv", ".md", ".txt", ".out", ".json", ".jsonl"})
_BUILD_GENERATED_TEXT = frozenset({"install_manifest.txt", "CMakeCache.txt", "compile_commands.json"})
# The file the workspace's index section lives in.
_OWN_FILES = frozenset({INDEX})
# Binary measurements whose handling is not settled: left and listed.
_UNSETTLED_PREFIXES = ("perf.data",)
_MAGIC = (
    (b"\x7fELF", "ELF"), (b"!<arch>\n", "ar archive"), (b"\x89PNG", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"), (b"GIF8", "GIF image"), (b"\x1a\x45\xdf\xa3", "Matroska/WebM video"),
    (b"RIFF", "RIFF (AVI/WAV)"), (b"\x1f\x8b", "gzip"), (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zstd"), (b"PK\x03\x04", "zip"), (b"BZh", "bzip2"),
    (b"SQLite format 3\x00", "SQLite database"), (b"GGUF", "GGUF model"), (b"ID3", "MP3 audio"),
    (b"\xff\xfb", "MP3 audio"), (b"OggS", "Ogg audio/video"), (b"fLaC", "FLAC audio"), (b"%PDF", "PDF"),
)
_ELF_TYPES = {1: "ELF object", 2: "ELF executable", 3: "ELF shared object or PIE executable", 4: "core file"}


@dataclass(frozen=True)
class Ages:
    """The clock, in seconds since a piece's newest modification."""

    archive: float
    gzip: float
    tar: float


@dataclass
class Report:
    """What one run did to one workspace, or would do in a dry run."""

    workspace: Path
    archive: Path
    freed: int = 0
    removed: int = 0
    moved: int = 0
    archived: list[str] = field(default_factory=list)
    gzipped: list[str] = field(default_factory=list)
    tarred: list[str] = field(default_factory=list)
    patches: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    left: list[tuple[Path, str]] = field(default_factory=list)
    # Pieces whose checkouts the rules could not settle, for the cleanup agent.
    checkouts: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def log_file() -> Path:
    """The one log of every deletion and move, of every run and workspace."""
    return _paths.archive_root() / "cleanup.log"


def archive_area(workspace: Path) -> Path:
    """The archive area of ``workspace``: `<home>/archive/work` for the
    home's work directory, `<home>/archive/<slug>` for any other."""
    root = _paths.archive_root()
    if workspace == _paths.work_dir().resolve(strict=False):
        return root / "work"
    return root / slug(workspace)


def workspaces(entries: list[str]) -> list[Path]:
    """The directories `CLEANUP_DIRS` names, absolute; an entry ending `/*`
    names each directory inside it. No entries names the home's work
    directory. Raises ValueError for an entry that is not absolute or ~."""
    if not entries:
        return [_paths.work_dir()]
    out: list[Path] = []
    for entry in entries:
        text = str(entry).strip()
        each = text.endswith("/*")
        path = Path(os.path.expanduser(text[:-2] if each else text))
        if not text or not path.is_absolute():
            raise ValueError(f"CLEANUP_DIRS: {entry!r} is not an absolute or ~ path")
        path = path.resolve(strict=False)
        if each:
            out.extend(sorted(child for child in path.iterdir() if child.is_dir() and not child.name.startswith(".")))
        else:
            out.append(path)
    return list(dict.fromkeys(out))


# Byte order marks of the UTF encodings whose text holds NUL bytes.
_WIDE_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")


def is_text(path: Path) -> bool:
    """Text by git's rule: no NUL byte in the first 8 KiB, or a UTF-16 or
    UTF-32 byte order mark. An empty file is text."""
    with path.open("rb") as stream:
        head = stream.read(8192)
    return b"\0" not in head or head.startswith(_WIDE_BOMS)


def kind(path: Path) -> str:
    """What a binary file is, from its first bytes."""
    try:
        with path.open("rb") as stream:
            head = stream.read(32)
    except OSError:
        return "binary"
    if head.startswith(b"\x7fELF") and len(head) >= 18:
        order = "little" if head[5] == 1 else "big"
        return _ELF_TYPES.get(int.from_bytes(head[16:18], order), "ELF")
    if head.startswith(b"\x89PNG") and len(head) >= 24:
        return f"PNG image {int.from_bytes(head[16:20], 'big')}x{int.from_bytes(head[20:24], 'big')}"
    if head[4:8] == b"ftyp":
        return "MP4/QuickTime video"
    for magic, name in _MAGIC:
        if head.startswith(magic):
            return name
    return "binary"


# ELF machines of GPU device code: a build's intermediate code objects
# (`-save-temps`, offload bundles), not programs anyone runs.
_DEVICE_MACHINES = frozenset({190, 224})  # EM_CUDA, EM_AMDGPU


def _finished_binary(path: Path) -> bool:
    """A program or shared library a build produced: an ELF executable or
    shared object for a host machine."""
    if kind(path) not in ("ELF executable", "ELF shared object or PIE executable"):
        return False
    with path.open("rb") as stream:
        head = stream.read(20)
    return int.from_bytes(head[18:20], "little" if head[5] == 1 else "big") not in _DEVICE_MACHINES


def _stamp(stat: os.stat_result, arrived: bool) -> float:
    return max(stat.st_mtime, stat.st_ctime) if arrived else stat.st_mtime


def newest(path: Path, *, arrived: bool = False) -> float:
    """The newest modification time of the files under ``path`` (symlinks
    not followed, directory times not counted: removing junk changes them),
    or of ``path`` itself when it holds no file. With ``arrived``, a file's
    change time counts too, so a tree copied in with its old times kept is
    as new as the copy."""
    try:
        if path.is_symlink() or path.is_file():
            return _stamp(path.lstat(), arrived)
        best = None
        for root, _dirs, files in os.walk(path):
            for name in files:
                with _quiet():
                    mtime = _stamp(os.lstat(os.path.join(root, name)), arrived)
                    best = mtime if best is None else max(best, mtime)
        return best if best is not None else _stamp(path.lstat(), arrived)
    except OSError:
        return time.time()


def size(path: Path) -> int:
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                with _quiet():
                    total += os.lstat(os.path.join(root, name)).st_size
        return total
    except OSError:
        return 0


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, kind_, exc, tb):
        return isinstance(exc, OSError)


def _now() -> float:
    return time.time()


def _date(stamp: float) -> str:
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")


def _human(count: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if count < 1024 or unit == "GiB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} GiB"


class Cleanup:
    """One workspace's run: `run` applies the rules, or with ``dry`` only
    says what it would do."""

    def __init__(self, workspace: Path, ages: Ages, *, dry: bool = False,
                 now: float | None = None, archive: Path | None = None, log: Path | None = None) -> None:
        self.workspace = workspace.resolve(strict=False)
        self.area = (archive or archive_area(self.workspace)).resolve(strict=False)
        self.log = (log or log_file()).resolve(strict=False)
        self.ages = ages
        self.dry = dry
        self.now = _now() if now is None else now
        self.report = Report(self.workspace, self.area)
        self.tracked = self._tracked(self.workspace)

    # --- guards and bookkeeping ---------------------------------------

    def _inside(self, path: Path) -> Path:
        """``path`` when it is inside the workspace or its archive area;
        anything else is refused."""
        absolute = Path(os.path.abspath(path))
        for root in (self.workspace, self.area):
            if absolute != root and absolute.is_relative_to(root):
                return absolute
        raise PermissionError(f"cleanup refuses {absolute}: outside {self.workspace} and {self.area}")

    def _log(self, action: str, path: Path, rule: str, destination: Path | None = None) -> None:
        """One line of the log: date, what happened, where the path was,
        where it went, the rule."""
        went = f"  ->  {destination}" if destination is not None else ""
        self.report.lines.append(f"{_date(self.now)}  {action}  {path}{went}  {rule}")

    def _remove(self, path: Path, reason: str) -> None:
        path = self._inside(path)
        amount = size(path)
        if not self.dry:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        self.report.freed += amount
        self.report.removed += 1
        self._log("deleted", path, f"{reason}, {_human(amount)}")

    def _move(self, source: Path, target: Path, rule: str) -> None:
        source, target = self._inside(source), self._inside(target)
        if not self.dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(source, target)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                staged = target.with_name(f".{target.name}.cleanup-new")
                if source.is_dir():
                    shutil.copytree(source, staged, symlinks=True)
                else:
                    shutil.copy2(source, staged, follow_symlinks=False)
                os.rename(staged, target)
                shutil.rmtree(source) if source.is_dir() else source.unlink()
        self.report.moved += 1
        self._log("moved", source, rule, target)

    def _record(self, piece: str) -> dict:
        try:
            return json.loads((self.area / RECORDS / f"{piece}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_record(self, piece: str, record: dict) -> None:
        if self.dry:
            return
        path = self._inside(self.area / RECORDS / f"{piece}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f".{path.name}.tmp")
        staged.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(staged, path)

    @staticmethod
    def _tracked(directory: Path) -> set[Path]:
        """The files a git repository holding ``directory`` tracks under it."""
        try:
            out = subprocess.run(["git", "--no-optional-locks", "-C", str(directory), "ls-files", "-z", "--full-name"],
                                 capture_output=True, timeout=60, check=False)
            top = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            return set()
        if out.returncode or top.returncode:
            return set()
        root = Path(top.stdout.strip())
        return {(root / name.decode("utf-8", "surrogateescape")).resolve(strict=False)
                for name in out.stdout.split(b"\0") if name}

    # --- the run ----------------------------------------------------------

    def run(self) -> Report:
        """Finish any interrupted stage change, move each piece of work to
        the stage its age calls for, then write the indexes and the log."""
        if not self.dry:
            self._recover()
        for entry in sorted(self.workspace.iterdir()):
            self._attempt(entry, self._live_entry)
        if self.area.is_dir():
            for entry in sorted(self.area.iterdir()):
                self._attempt(entry, self._archived_entry)
        if not self.dry:
            self._write_log()
            self.write_indexes()
        return self.report

    def _attempt(self, entry: Path, step) -> None:
        try:
            step(entry)
        except PermissionError as exc:
            # Not this run's to read: it stays as it is, for the operator.
            self.report.left.append((entry, f"cannot be read: {exc}"))
        except OSError as exc:
            self.report.failed.append((entry, str(exc)))

    def _live_entry(self, entry: Path) -> None:
        name = entry.name
        if name.startswith(".") or name in _OWN_FILES or entry.is_symlink():
            return
        resolved = entry.resolve(strict=False)
        if self.workspace == _paths.work_dir().resolve(strict=False) and resolved == _paths.notes_dir().resolve(strict=False):
            return
        if resolved in self.tracked or self.area.is_relative_to(resolved):
            return
        age = self.now - newest(entry, arrived=True)
        if age < self.ages.archive:
            return
        if entry.is_file():
            self._loose_file(entry)
        elif entry.is_dir():
            self._archive_piece(entry)

    def _loose_file(self, path: Path) -> None:
        if not is_text(path):
            self._remove(path, f"binary: {kind(path)}")
            return
        group = f"loose-{_date(path.lstat().st_mtime)}"
        target = self.area / group / path.name
        if target.exists() or (self.area / f"{group}.tar").exists():
            self.report.left.append((path, f"{target} exists"))
            return
        record = self._record(group) or {"piece": group, "from": f"loose files at the top of {self.workspace}",
                                          "last_modified": _date(path.lstat().st_mtime), "archived": _date(self.now)}
        self._save_record(group, record)
        self._move(path, target, f"text, unmodified {self.ages.archive / _DAY:g} days, gathered by day")

    # --- day 30 -----------------------------------------------------------

    def _archive_piece(self, piece: Path) -> None:
        target = self.area / piece.name
        if target.exists() or (self.area / f"{piece.name}.tar").exists():
            self.report.left.append((piece, f"{target} already exists in the archive"))
            return
        if any(path.is_relative_to(piece.resolve(strict=False)) for path in self.tracked):
            self.report.left.append((piece, "holds files the workspace's git repository tracks"))
            return
        record = {"removed": [], "kept_from_builds": [], "patches": [], "retired": [], **self._record(piece.name)}
        record.update(piece=piece.name, last_modified=_date(newest(piece)))
        record.setdefault("from", str(piece))
        settled = self._clean_tree(piece, piece, record)
        if not settled:
            # What went stays on record for the day the piece is archived. The
            # agent is asked once about a piece (`asked`); again once it changes.
            if record.get("agent_asked") != newest(piece, arrived=True):
                self.report.checkouts.append(piece)
            self._save_record(piece.name, record)
            self.report.left.append((piece, "holds a source checkout the rules could not settle; it stays live"))
            return
        record["archived"] = _date(self.now)
        self._save_record(piece.name, record)
        self._move(piece, target, f"unmodified {self.ages.archive / _DAY:g} days")
        self.report.archived.append(piece.name)

    def _clean_tree(self, piece: Path, directory: Path, record: dict) -> bool:
        """Remove the junk under ``directory``; False when a source checkout
        under it could not be settled, so the piece must stay live."""
        settled = True
        if self._is_checkout(directory) and directory != piece:
            return self._checkout(piece, directory, record)
        if directory == piece and self._is_checkout(directory):
            return False
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            self.report.failed.append((directory, str(exc)))
            return False
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name == ".git":
                    continue
                if self._is_checkout(entry):
                    settled = self._clean_tree(piece, entry, record) and settled
                elif self._junk_tree(entry):
                    self._remove_tree(entry, record, f"regenerable tree: {entry.name}")
                elif self._is_build(entry):
                    self._build_directory(piece, entry, record)
                else:
                    settled = self._clean_tree(piece, entry, record) and settled
            elif entry.is_file():
                self._file(entry, record)
        return settled

    def _junk_tree(self, path: Path) -> bool:
        if path.name in _JUNK_TREES or (path / "pyvenv.cfg").is_file():
            return True
        return path.name == "target" and (path.parent / "Cargo.toml").is_file()

    @staticmethod
    def _is_build(path: Path) -> bool:
        """A build directory: it holds what a build writes and none of what a
        source tree holds, so an in-tree build is never one."""
        return (any((path / marker).exists() for marker in _BUILD_MARKERS)
                and not any((path / marker).exists() for marker in _SOURCE_MARKERS))

    @staticmethod
    def _is_checkout(path: Path) -> bool:
        return (path / ".git").exists()

    def _file(self, path: Path, record: dict) -> None:
        if path.resolve(strict=False) in self.tracked:
            return
        if path.name.startswith(_UNSETTLED_PREFIXES):
            self.report.left.append((path, "binary measurement, not settled"))
            return
        try:
            text = is_text(path)
        except OSError as exc:
            self.report.failed.append((path, str(exc)))
            return
        if not text:
            what = kind(path)
            record["removed"].append({"path": str(path.relative_to(self.workspace)), "bytes": size(path), "kind": what})
            self._remove(path, f"binary: {what}")

    def _remove_tree(self, path: Path, record: dict, reason: str) -> None:
        record["removed"].append({"path": str(path.relative_to(self.workspace)), "bytes": size(path), "kind": reason})
        self._remove(path, reason)

    def _build_kept(self, build: Path) -> list[Path]:
        """What a build directory gives up before it goes: the files at its
        top that say how it was configured, the compiler identification
        CMake wrote, generated build information, ctest's log, the text an
        agent left by running from it, and its finished programs and shared
        libraries."""
        kept: list[Path] = []
        for name in _BUILD_TOP_KEPT:
            if (build / name).is_file():
                kept.append(build / name)
        kept.extend(sorted((build / "CMakeFiles").glob("*/CMake*Compiler.cmake")))
        kept.extend(sorted((build / "Testing" / "Temporary").glob("LastTest.log*")))
        kept.extend(sorted(path for path in (build / "meson-info").glob("*") if path.is_file()))
        for root, dirs, files in os.walk(build):
            here = Path(root)
            # Generated trees, and the builds nested inside this one.
            dirs[:] = [name for name in dirs if name != "CMakeFiles" and not name.endswith("-prefix")
                       and not (here / name / "CMakeCache.txt").exists() and name != "meson-info"]
            for name in files:
                path = here / name
                if path.is_symlink() or path in kept:
                    continue
                if "build-info" in name or "build_info" in name:
                    kept.append(path)
                    continue
                try:
                    if is_text(path):
                        if path.suffix in _AGENT_TEXT_SUFFIXES and name not in _BUILD_GENERATED_TEXT:
                            kept.append(path)
                    elif _finished_binary(path):
                        kept.append(path)
                except OSError:
                    continue
        # Symlinks to kept libraries (libfoo.so -> libfoo.so.1) come too.
        for root, _dirs, files in os.walk(build):
            for name in files:
                path = Path(root) / name
                if path.is_symlink() and (path.parent / os.readlink(path)).resolve(strict=False) in {
                        item.resolve(strict=False) for item in kept}:
                    kept.append(path)
        return list(dict.fromkeys(kept))

    def _build_directory(self, piece: Path, build: Path, record: dict) -> None:
        kept = self._build_kept(build)
        destination = piece / BUILD_KEPT / build.relative_to(piece)
        for path in kept:
            target = self._inside(destination / path.relative_to(build))
            if not self.dry:
                target.parent.mkdir(parents=True, exist_ok=True)
                if path.is_symlink():
                    target.unlink(missing_ok=True)
                    target.symlink_to(os.readlink(path))
                    # The link's own time, so the clock goes on from the piece's.
                    link = path.lstat()
                    os.utime(target, (link.st_atime, link.st_mtime), follow_symlinks=False)
                else:
                    shutil.copy2(path, target)
            record["kept_from_builds"].append(str(target.relative_to(piece)))
        # What was copied out is not freed.
        copied = sum(size(path) for path in kept if not path.is_symlink())
        self._remove_tree(build, record, "build directory")
        self.report.freed -= copied

    # --- source checkouts -------------------------------------------------

    @staticmethod
    def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess:
        # No optional locks: `git status` would otherwise refresh the index,
        # which for a linked worktree is inside the owner's repository.
        return subprocess.run(["git", "--no-optional-locks", "-C", str(checkout), *args],
                              capture_output=True, timeout=300, check=False)

    def _checkout(self, piece: Path, checkout: Path, record: dict) -> bool:
        """Settle a linked worktree whose repository is outside the
        workspace: clean its junk, write a patch of what the repository
        lacks, retire it. False for any other checkout, and for one git
        cannot compare."""
        # Junk inside the checkout goes first, whatever becomes of the checkout:
        # what is left is what counts.
        for entry in sorted(checkout.iterdir()):
            if entry.is_dir() and not entry.is_symlink() and entry.name != ".git":
                if self._junk_tree(entry):
                    self._remove_tree(entry, record, f"regenerable tree: {entry.name}")
                elif self._is_build(entry):
                    self._build_directory(piece, entry, record)
        if not (checkout / ".git").is_file():
            return False
        listing = self._git(checkout, "worktree", "list", "--porcelain")
        if listing.returncode:
            return False
        first = listing.stdout.decode("utf-8", "replace").split("\n\n", 1)[0].splitlines()
        owner = Path(first[0].removeprefix("worktree ")) if first and first[0].startswith("worktree ") else None
        owner_head = next((line.removeprefix("HEAD ") for line in first if line.startswith("HEAD ")), None)
        if owner is None or owner_head is None or owner.resolve(strict=False).is_relative_to(self.workspace):
            return False
        if (checkout / ".gitmodules").is_file():
            return False
        branch = self._git(checkout, "symbolic-ref", "--short", "HEAD").stdout.decode().strip()
        refs = self._git(checkout, "for-each-ref", "--format=%(refname)", "refs/heads").stdout.decode().split()
        others = [ref for ref in refs if ref != f"refs/heads/{branch}"]
        missing = self._git(checkout, "rev-list", "HEAD", "--not", *others).stdout.decode().split()
        status = self._git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored")
        if status.returncode:
            return False
        changed, untracked = False, []
        for item in status.stdout.decode("utf-8", "surrogateescape").split("\0"):
            if len(item) < 4:
                continue
            code, name = item[:2], item[3:]
            path = checkout / name
            if code in ("??", "!!"):
                if path.is_file() and not path.is_symlink() and is_text(path):
                    untracked.append(name)
            else:
                changed = True
        if missing or changed or untracked:
            patch = self._patch(piece, checkout, owner_head, bool(missing), changed, untracked)
            record["patches"].append(str(patch.relative_to(piece)))
            self.report.patches.append(str(patch))
        if not self.dry:
            retired = self._git(checkout, "worktree", "remove", "--force", str(checkout))
            if retired.returncode:
                self.report.failed.append((checkout, retired.stderr.decode("utf-8", "replace").strip()))
                return False
        record["retired"].append({"path": str(checkout.relative_to(self.workspace)), "repository": str(owner),
                                  "branch": branch or None})
        self.report.retired.append(str(checkout))
        self._log("retired", checkout, f"linked worktree of {owner}" + (f", branch {branch} kept" if branch else "")
                  + (", patch written" if missing or changed or untracked else ", its repository has its work"))
        return True

    def _patch(self, piece: Path, checkout: Path, owner_head: str, commits: bool, changed: bool,
               untracked: list[str]) -> Path:
        """Write what the owner's repository lacks of ``checkout`` beside the
        piece's results, its modification time the piece's, so the clock
        does not restart."""
        name = "-".join(checkout.relative_to(piece).parts) or checkout.name
        patch = self._inside(piece / f"{name}-worktree.patch")
        if self.dry:
            return patch
        parts: list[bytes] = []
        if commits:
            base = self._git(checkout, "merge-base", "HEAD", owner_head).stdout.decode().strip() or owner_head
            parts.append(self._git(checkout, "format-patch", "--stdout", f"{base}..HEAD").stdout)
        if changed:
            parts.append(self._git(checkout, "diff", "HEAD").stdout)
        for name_ in untracked:
            parts.append(self._git(checkout, "diff", "--no-index", "/dev/null", name_).stdout)
        stamp = newest(piece)
        patch.write_bytes(b"".join(parts))
        os.utime(patch, (stamp, stamp))
        return patch

    # --- days 60 and 90 ---------------------------------------------------

    def _archived_entry(self, entry: Path) -> None:
        if entry.name.startswith(".") or entry.name in _OWN_FILES or entry.name in (RECORDS, DESCRIPTIONS):
            return
        if not entry.is_dir() or entry.is_symlink():
            return
        age = self.now - newest(entry)
        record = self._record(entry.name)
        if age >= self.ages.gzip and not record.get("gzipped"):
            self._gzip(entry)
            record["gzipped"] = _date(self.now)
            self._save_record(entry.name, record)
            self.report.gzipped.append(entry.name)
        if age >= self.ages.tar:
            self._tar(entry)
            record["tarred"] = _date(self.now)
            self._save_record(entry.name, record)
            self.report.tarred.append(entry.name)

    def _gzip(self, piece: Path) -> None:
        """Each file of ``piece`` gzipped, one `.gz` per file, built beside it
        and swapped in, modification times kept."""
        self._log("gzipped", piece, f"unmodified {self.ages.gzip / _DAY:g} days")
        if self.dry:
            return
        new = self._inside(piece.with_name(f".{piece.name}.cleanup-new"))
        old = self._inside(piece.with_name(f".{piece.name}.cleanup-old"))
        if new.exists():
            shutil.rmtree(new)
        for root, dirs, files in os.walk(piece):
            here = Path(root)
            mirror = new / here.relative_to(piece)
            mirror.mkdir(parents=True, exist_ok=True)
            for name in files:
                source = here / name
                if source.is_symlink():
                    link = os.readlink(source)
                    pointed = here / link
                    if not os.path.isabs(link) and pointed.is_file() and not link.endswith(".gz"):
                        link += ".gz"
                    made = mirror / (name if name.endswith(".gz") else f"{name}.gz")
                    made.symlink_to(link)
                    stat = source.lstat()
                    os.utime(made, (stat.st_atime, stat.st_mtime), follow_symlinks=False)
                    continue
                stat = source.lstat()
                if name.endswith(".gz"):
                    target = mirror / name
                    shutil.copy2(source, target)
                else:
                    target = mirror / f"{name}.gz"
                    with source.open("rb") as raw, gzip.GzipFile(target, "wb", mtime=int(stat.st_mtime)) as packed:
                        shutil.copyfileobj(raw, packed, 1 << 20)
                    shutil.copymode(source, target)
                os.utime(target, (stat.st_atime, stat.st_mtime))
        os.rename(piece, old)
        os.rename(new, piece)
        shutil.rmtree(old)

    def _tar(self, piece: Path) -> None:
        """``piece`` as one `.tar` beside it, written aside and renamed into
        place before the directory goes."""
        tar = self._inside(piece.with_name(f"{piece.name}.tar"))
        self._log("tarred", piece, f"unmodified {self.ages.tar / _DAY:g} days", tar)
        if self.dry:
            return
        staged = self._inside(piece.with_name(f".{piece.name}.tar.tmp"))
        stamp = newest(piece)
        with tarfile.open(staged, "w") as archive:
            archive.add(piece, arcname=piece.name)
        os.utime(staged, (stamp, stamp))
        os.rename(staged, tar)
        shutil.rmtree(piece)

    def _recover(self) -> None:
        """Finish or undo a stage change an interrupted run left half done."""
        if not self.area.is_dir():
            return
        for entry in list(self.area.iterdir()):
            if not entry.exists() and not entry.is_symlink():
                continue  # handled with its pair
            name = entry.name
            if name.startswith(".") and name.endswith(".cleanup-new"):
                piece = self.area / name[1:-len(".cleanup-new")]
                old = self.area / f".{piece.name}.cleanup-old"
                if old.exists() and not piece.exists():
                    os.rename(entry, piece)
                    shutil.rmtree(old)
                elif not old.exists():
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            elif name.startswith(".") and name.endswith(".cleanup-old"):
                piece = self.area / name[1:-len(".cleanup-old")]
                if piece.exists():
                    shutil.rmtree(entry)
                elif not (self.area / f".{piece.name}.cleanup-new").exists():
                    os.rename(entry, piece)
            elif name.startswith(".") and name.endswith(".tar.tmp"):
                entry.unlink()
            elif name.endswith(".tar") and (self.area / name[:-4]).is_dir():
                shutil.rmtree(self.area / name[:-4])

    # --- the log and the indexes -----------------------------------------

    def _write_log(self) -> None:
        if not self.report.lines:
            return
        log = self.log
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write("".join(f"{line}\n" for line in self.report.lines))

    def _stage(self, path: Path) -> str:
        if path.suffix == ".tar":
            return "tarred"
        return "gzipped" if self._record(path.name).get("gzipped") else "archived"

    def _files(self, path: Path) -> list[tuple[str, int]]:
        if path.suffix == ".tar":
            with tarfile.open(path) as archive:
                return [(member.name, member.size) for member in archive.getmembers() if member.isfile()]
        out = []
        for root, _dirs, files in os.walk(path):
            for name in sorted(files):
                full = Path(root) / name
                with _quiet():
                    out.append((str(full.relative_to(path.parent)), full.lstat().st_size))
        return sorted(out)

    def _archive_index(self) -> str:
        days = {key: f"{getattr(self.ages, key) / _DAY:g}" for key in ("archive", "gzip", "tar")}
        lines = [
            f"# Archived work from {self.workspace}",
            "",
            "Read this first. Each section is one piece of work that left the workspace. Its stage says how to "
            f"read it: archived (after {days['archive']} days unmodified) is plain files, read them as they are; "
            f"gzipped (after {days['gzip']} days) has one `.gz` per file, search with `zgrep -r PATTERN DIR` and "
            f"read with `zcat FILE.gz`; tarred (after {days['tar']} days) is one `.tar`, its files are listed "
            "below, read one with `tar -xOf PIECE.tar PATH | zcat`. Junk was removed when a piece was archived: "
            f"what went is under each piece and in `{self.log}`. A build directory's configuration, compiler "
            f"identification and finished binaries are under `{BUILD_KEPT}/`.",
            "",
        ]
        for path in sorted(self.area.iterdir()):
            if path.name.startswith(".") or path.name in _OWN_FILES or path.name in (RECORDS, DESCRIPTIONS):
                continue
            if not (path.is_dir() or path.suffix == ".tar"):
                continue
            name = path.name[:-4] if path.suffix == ".tar" else path.name
            record = self._record(name)
            files = self._files(path)
            lines += [f"## {name}", "",
                      f"- stage: {self._stage(path)}",
                      f"- path: {path}",
                      f"- came from: {record.get('from', 'unknown')}",
                      f"- last modified: {record.get('last_modified', 'unknown')}; archived {record.get('archived', 'unknown')}"
                      + (f"; gzipped {record['gzipped']}" if record.get("gzipped") else "")
                      + (f"; tarred {record['tarred']}" if record.get("tarred") else ""),
                      f"- size: {_human(sum(amount for _, amount in files))} in {len(files)} files"]
            description = self.area / DESCRIPTIONS / f"{name}.md"
            if description.is_file():
                lines += ["", description.read_text(encoding="utf-8", errors="replace").strip()]
            else:
                lines += ["- description: not written yet"]
            for key, title in (("kept_from_builds", "kept from build directories"), ("patches", "patches")):
                if record.get(key):
                    lines += [f"- {title}: " + ", ".join(f"`{item}`" for item in record[key])]
            if record.get("retired"):
                lines += ["- retired worktrees: " + ", ".join(
                    f"`{item['path']}` of {item['repository']}" + (f" (branch {item['branch']})" if item.get("branch") else "")
                    for item in record["retired"])]
            if record.get("removed"):
                removed = record["removed"]
                lines += [f"- removed when archived: {len(removed)} items, "
                          f"{_human(sum(item['bytes'] for item in removed))}"]
                lines += [f"  - `{item['path']}` {_human(item['bytes'])}, {item['kind']}" for item in removed[:LISTED_FILES_MAX]]
            lines += ["- files:"]
            lines += [f"  - `{name_}` {_human(amount)}" for name_, amount in files[:LISTED_FILES_MAX]]
            if len(files) > LISTED_FILES_MAX:
                lines += [f"  - ... {len(files) - LISTED_FILES_MAX} more"]
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _live_section(self) -> str:
        days = f"{self.ages.archive / _DAY:g}"
        lines = [SECTION_START, "## Live work (kept by js cleanup)", "",
                 f"Work unmodified for {days} days moves to {self.area}; its index is "
                 f"{self.area / INDEX}.", ""]
        for entry in sorted(self.workspace.iterdir()):
            if entry.name.startswith(".") or entry.name in _OWN_FILES or not entry.is_dir() or entry.is_symlink():
                continue
            stamp = newest(entry)
            lines.append(f"- `{entry.name}/` last modified {_date(stamp)}, {_human(size(entry))}")
        lines.append(SECTION_END)
        return "\n".join(lines) + "\n"

    def write_indexes(self) -> None:
        """The archive area's index, whole, and the live section of the
        workspace's index; the rest of that file stays as written."""
        if self.area.is_dir():
            self._replace(self.area / INDEX, self._archive_index())
        index = self.workspace / INDEX
        current = index.read_text(encoding="utf-8") if index.is_file() else ""
        section = self._live_section()
        if SECTION_START in current and SECTION_END in current:
            head, rest = current.split(SECTION_START, 1)
            updated = head + section + rest.split(SECTION_END, 1)[1].lstrip("\n")
        else:
            updated = (current.rstrip() + "\n\n" if current.strip() else "") + section
        self._replace(index, updated)

    def _replace(self, path: Path, text: str) -> None:
        """Write ``text`` to ``path`` whole, and only when it changes it."""
        path = self._inside(path)
        with _quiet():
            if path.read_text(encoding="utf-8") == text:
                return
        staged = path.with_name(f".{path.name}.tmp")
        staged.write_text(text, encoding="utf-8")
        os.replace(staged, path)

    def asked(self, pieces: list[Path]) -> None:
        """Note that the agent was asked about ``pieces`` as they are now."""
        for piece in pieces:
            record = self._record(piece.name)
            record["agent_asked"] = newest(piece, arrived=True)
            self._save_record(piece.name, record)

    def undescribed(self) -> list[Path]:
        """Archived pieces with no description yet."""
        if not self.area.is_dir():
            return []
        out = []
        for path in sorted(self.area.iterdir()):
            if path.name.startswith(".") or path.name in _OWN_FILES or path.name in (RECORDS, DESCRIPTIONS):
                continue
            name = path.name[:-4] if path.suffix == ".tar" else path.name
            if (path.is_dir() or path.suffix == ".tar") and not (self.area / DESCRIPTIONS / f"{name}.md").is_file():
                out.append(path)
        return out
