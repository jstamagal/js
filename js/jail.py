"""`js -C DIR`: the jail that keeps the agent's tools inside DIR.

The jail keeps the agent's context clean. It is not a defence against a
hostile model. Under `-C`:

- every command a tool starts (shell, kernel, terminal, wiki converters) runs
  under bubblewrap. The system is read-only. `/home` and the operator's home
  are empty tmpfs mounts, and so are `/run/user` and every network
  filesystem mount. The jail's `/tmp` and `~/.js/tmp` are directories private
  to this js process. DIR is bound read-write at its real path. The PATH
  directories and the kernel's interpreter under a hidden tree or the host's
  `/tmp`, the `jail.bind` entries and the `/add` binds are bound back. The
  network is shared.
- the file tools resolve every path and refuse one outside DIR and the bound
  paths (`confine`). Their results name the jail's /tmp and ~/.js/tmp as the
  jail shows them, not the private directory behind them (`shown`).

The jail is process-wide: subagents run in this process and use the same one.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import pwd
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

# Filesystem types whose mounts are hidden in the jail. `find /` over an NFS
# home is the sweep the jail exists to stop.
NETWORK_FS = frozenset({
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "sshfs", "fuse.sshfs", "fuse.rclone",
    "9p", "afs", "ceph", "glusterfs", "fuse.glusterfs", "davfs", "fuse.s3fs",
})

# Runs the jailed command with SIGINT ignored in bwrap itself. The kernel tool
# interrupts a cell by sending SIGINT to the kernel's process group, which
# includes bwrap; bwrap would die of it and take the kernel with it. ipykernel
# installs its own SIGINT handler around each cell, so the cell still gets it.
_IGNORE_SIGINT = ["/bin/sh", "-c", 'trap "" INT; exec "$@"', "sh"]


class JailError(Exception):
    """A path the jail does not let a tool reach. The message is one line."""


class Refusal(str):
    """A tool result that reports a JailError. Retrying the call cannot change
    it, so the runtime gives it no retry count."""


@dataclass(frozen=True)
class Bind:
    path: Path
    rw: bool = False

    def spec(self) -> str:
        return f"{self.path}:rw" if self.rw else str(self.path)


def parse_bind(spec: str) -> Bind:
    """``path``, ``path:ro`` or ``path:rw``. The path is absolute or starts
    with ``~``. Raises ValueError."""
    text = str(spec).strip()
    rw = False
    for suffix, writable in ((":rw", True), (":ro", False)):
        if text.endswith(suffix):
            text, rw = text[: -len(suffix)], writable
            break
    if not text:
        raise ValueError(f"empty bind: {spec!r}")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        raise ValueError(f"bind path must be absolute or start with ~: {spec!r}")
    return Bind(Path(os.path.abspath(path)), rw)


def parse_binds(specs) -> list[Bind]:
    """The `jail.bind` setting's value as binds. Raises ValueError."""
    if not isinstance(specs, (list, tuple)):
        raise ValueError("jail.bind is a JSON list of \"path[:rw]\" strings")
    return [parse_bind(item) for item in specs]


def _real(path: Path) -> Path:
    """``path`` with symlinks resolved, or as given when it cannot be."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def reach(path: Path) -> list[Path]:
    """The paths to bind so ``path`` resolves in the jail as it does on the
    host: each symlinked directory met on the way, where it is met, and the
    path it finally resolves to."""
    out: list[Path] = []
    current = Path(os.path.abspath(path))
    for _ in range(40):
        walked = Path(current.anchor)
        for index, part in enumerate(current.parts[1:], 1):
            walked = walked / part
            if walked.is_symlink():
                rest = current.parts[index + 1:]
                if rest:
                    out.append(walked)
                current = Path(os.path.normpath(walked.parent / os.readlink(walked))).joinpath(*rest)
                break
        else:
            out.append(current)
            return out
    return out


def operator_homes() -> list[Path]:
    """The operator's home, by $HOME and by the password database."""
    homes = [_real(paths.user_home())]
    with contextlib.suppress(KeyError, OSError):
        homes.append(_real(Path(pwd.getpwuid(os.getuid()).pw_dir)))
    return list(dict.fromkeys(homes))


@dataclass(frozen=True)
class _Mount:
    device: str          # major:minor
    root: Path           # the directory of the filesystem mounted here
    point: Path
    fstype: str


def _unescape(text: str) -> str:
    """Undo mountinfo's octal escapes (`\\040` is a space)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _mounts() -> list[_Mount]:
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return []
    mounts: list[_Mount] = []
    for line in lines:
        left, sep, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if not sep or len(fields) < 5 or not tail:
            continue
        mounts.append(_Mount(fields[2], Path(_unescape(fields[3])), Path(_unescape(fields[4])), tail[0]))
    return mounts


def _other_views(tree: Path, mounts: list[_Mount]) -> list[Path]:
    """Other paths that show the same directory as ``tree``: a home on a btrfs
    subvolume is also reachable where the whole filesystem is mounted."""
    holders = [m for m in mounts if _under(tree, m.point)]
    if not holders:
        return []
    holder = max(holders, key=lambda m: len(m.point.parts))
    target = holder.root / tree.relative_to(holder.point)
    views: list[Path] = []
    for mount in mounts:
        if mount is holder or mount.device != holder.device or not _under(target, mount.root):
            continue
        views.append(mount.point / target.relative_to(mount.root))
    return views


def hidden_roots() -> list[Path]:
    """The trees the jail replaces with an empty tmpfs, outermost first:
    /home, the operator's home, /run/user, every network filesystem, and every
    other mount that shows one of those."""
    mounts = _mounts()
    base = [Path("/home"), *operator_homes(), Path("/run/user"),
            *(m.point for m in mounts if m.fstype in NETWORK_FS)]
    candidates = [*base, *(view for tree in base for view in _other_views(tree, mounts))]
    roots: list[Path] = []
    for candidate in sorted(dict.fromkeys(candidates), key=lambda p: len(p.parts)):
        if candidate == Path("/") or not candidate.is_dir():
            continue
        if any(_under(candidate, root) for root in roots):
            continue
        roots.append(candidate)
    return roots


@dataclass
class Jail:
    root: Path
    bwrap: str
    private: Path                       # holds the jail's /tmp and ~/.js/tmp
    added: list[Bind] = field(default_factory=list)   # /add binds

    @property
    def tmp(self) -> Path:
        return self.private / "tmp"

    @property
    def js_tmp(self) -> Path:
        return self.private / "js-tmp"

    # --- what is visible -------------------------------------------------

    def binds(self, setting: object = ()) -> list[Bind]:
        """The `jail.bind` entries plus the `/add` binds. Invalid entries in
        ``setting`` are skipped; `/set` refuses them."""
        out: list[Bind] = []
        for item in setting or ():
            with contextlib.suppress(ValueError):
                out.append(parse_bind(item))
        return [*out, *self.added]

    def _areas(self, setting: object) -> list[Bind]:
        """Every tree a file tool may reach, as the host sees it."""
        areas = [Bind(self.root, True), Bind(self.private, True)]
        areas += [Bind(_real(b.path), b.rw) for b in self.binds(setting)]
        areas.append(Bind(_real(paths.tool_results_dir()), False))
        areas += [Bind(p, False) for p in self._path_dirs(os.environ.get("PATH", ""))]
        return areas

    def _path_dirs(self, path_value: str) -> list[Path]:
        """PATH directories that sit in a hidden tree, resolved."""
        hidden = hidden_roots()
        out: list[Path] = []
        for entry in path_value.split(os.pathsep):
            if not entry or not os.path.isabs(entry):
                continue
            real = _real(Path(entry))
            if real.is_dir() and any(_under(real, h) for h in hidden):
                out.append(real)
        return list(dict.fromkeys(out))

    def _path_binds(self, path_value: str) -> list[Path]:
        """What to bind so every PATH directory resolves in the jail."""
        out: list[Path] = []
        for entry in path_value.split(os.pathsep):
            if entry and os.path.isabs(entry) and os.path.isdir(entry):
                out += reach(Path(entry))
        return out

    def host_path(self, path: Path, setting: object = ()) -> Path:
        """``path`` as the host sees it: under the jail's /tmp or ~/.js/tmp it
        names a file in the private directory, anywhere else itself."""
        path = Path(os.path.abspath(path))
        if any(_under(path, b.path) or _under(path, _real(b.path))
               for b in [Bind(self.root), *self.binds(setting)]):
            return path
        js_tmp = paths.user_home() / ".js" / "tmp"
        if _under(path, js_tmp):
            return self.js_tmp / path.relative_to(js_tmp)
        # A hidden tree stays hidden even where it sits under /tmp.
        if any(_under(path, root) for root in hidden_roots()):
            return path
        if _under(path, Path("/tmp")):
            return self.tmp / path.relative_to("/tmp")
        return path

    def shown(self, text: str) -> str:
        """``text`` with every host path into the jail's /tmp or ~/.js/tmp
        written as the jail shows it: ``/tmp/…``, and ``~/.js/tmp/…`` with the
        operator's home spelled out. The inverse of `host_path` for those two
        trees."""
        views: dict[str, str] = {}
        for private in dict.fromkeys([self.private, _real(self.private)]):
            views[str(private / "tmp")] = "/tmp"
            views[str(private / "js-tmp")] = str(paths.user_home() / ".js" / "tmp")
        # A host path ends where a file name character does not follow.
        pattern = "(" + "|".join(re.escape(p) for p in sorted(views, key=len, reverse=True)) + r")(?![\w.-])"
        return re.sub(pattern, lambda m: views[m.group(1)], text)

    def confine(self, path: Path, *, write: bool = False, follow: bool = True,
                setting: object = ()) -> Path:
        """The host path a file tool uses for ``path``. Raises JailError when
        the jail does not show it, or shows it read-only and ``write`` is set."""
        mapped = self.host_path(path, setting)
        real = _real(mapped) if follow else _real(mapped.parent) / mapped.name
        areas = self._areas(setting)
        # The innermost area containing the path decides, as the innermost
        # mount does inside the jail.
        best: Bind | None = None
        for area in areas:
            if _under(real, area.path) and (best is None or len(area.path.parts) >= len(best.path.parts)):
                best = area
        # The jail's /tmp and ~/.js/tmp hold only what this process's tools put
        # there; a missing path in them is a host path the jail does not show.
        private_miss = (not write and _under(mapped, self.private)
                        and not _under(Path(os.path.abspath(path)), self.private)
                        and not os.path.lexists(mapped))
        if best is None or private_miss:
            raise JailError(
                f"{path} is outside the jail: js -C keeps the tools in {self.root} and its bound paths"
            )
        if write and not best.rw:
            raise JailError(f"{path} is read-only in the jail (bound from {best.path})")
        return real

    def bound(self, path: Path, setting: object = ()) -> bool:
        """Whether ``path`` lies in DIR, a `jail.bind` entry or an /add bind."""
        real = _real(path)
        return any(_under(real, _real(b.path)) for b in [Bind(self.root), *self.binds(setting)])

    def add(self, bind: Bind) -> None:
        """Show ``bind`` from the next command on (/add). A path added again
        takes the new access."""
        self.added = [b for b in self.added if b.path != bind.path] + [bind]

    def drop(self, path: Path) -> bool:
        """Stop showing an /add bind. False when ``path`` was not added."""
        kept = [b for b in self.added if b.path != path]
        dropped = len(kept) != len(self.added)
        self.added = kept
        return dropped

    # --- bwrap -------------------------------------------------------------

    def argv(self, argv: list[str], *, cwd: Path | str | None = None, env_path: str = "",
             setting: object = (), extra_ro: tuple[Path, ...] = (),
             extra_rw: tuple[Path, ...] = (), ignore_sigint: bool = False) -> list[str]:
        """``argv`` wrapped to run in the jail.

        ``env_path`` is the PATH the command runs with; its directories under a
        hidden tree are bound back read-only. ``extra_ro``/``extra_rw`` bind
        more paths for this one command (the kernel's interpreter, its sockets)."""
        hidden = hidden_roots()
        home = paths.user_home()
        out = [self.bwrap, "--die-with-parent", "--unshare-ipc", "--unshare-uts",
               "--unshare-cgroup-try", "--ro-bind", "/", "/", "--dev", "/dev"]
        out += ["--bind", str(self.tmp), "/tmp"]
        for root in hidden:
            out += ["--tmpfs", str(root)]
        out += ["--bind", str(self.js_tmp), str(home / ".js" / "tmp"),
                "--bind", str(self.private), str(self.private)]

        explicit = [Bind(self.root, True), *self.binds(setting), *(Bind(Path(p), True) for p in extra_rw)]

        def covered(path: Path) -> bool:
            return any(_under(path, _real(b.path)) for b in explicit)

        implicit = [Bind(p, False) for p in self._path_binds(env_path)]
        implicit.append(Bind(_real(paths.tool_results_dir()), False))
        implicit += [Bind(p, False) for extra in extra_ro for p in reach(Path(extra))]
        # The jail's /tmp is private, so an interpreter or PATH directory under
        # the host's /tmp is bound back like one under a hidden tree. A root
        # itself on PATH is not bound back; that would undo the replacement.
        replaced = [*hidden, Path("/tmp")]
        implicit = [b for b in implicit
                    if any(_under(b.path, h) and b.path != h for h in replaced)
                    and not covered(b.path)]
        # Outer paths first, so a bind inside another lands on top of it.
        binds = sorted(dict.fromkeys([*implicit, *explicit]), key=lambda b: len(b.path.parts))
        for bind in binds:
            src = str(bind.path)
            out += ["--bind-try" if bind.rw else "--ro-bind-try", src, src]
        if cwd is not None:
            out += ["--chdir", str(cwd)]
        prefix = _IGNORE_SIGINT if ignore_sigint else []
        return [*prefix, *out, "--", *argv]

    def self_test(self) -> str | None:
        """None when a command runs in the jail, else bwrap's first error line."""
        try:
            proc = subprocess.run(
                self.argv(["true"], cwd=self.root), capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return str(exc)
        if proc.returncode == 0:
            return None
        detail = next((line.strip() for line in proc.stderr.splitlines() if line.strip()), "")
        return detail or f"exit {proc.returncode}"

    def cleanup(self) -> None:
        shutil.rmtree(self.private, ignore_errors=True)


ACTIVE: Jail | None = None


def active() -> Jail | None:
    return ACTIVE


def shown(text: str) -> str:
    """``text`` as `Jail.shown` writes it under a jail, else itself."""
    return text if ACTIVE is None else ACTIVE.shown(text)


def scratch_dir() -> Path:
    """Where js puts a scratch file a jailed command must reach: the jail's
    ~/.js/tmp (visible inside at the host path), else js's scratch directory."""
    if ACTIVE is not None:
        return ACTIVE.js_tmp
    return paths.tmp_dir()


def wrap(argv: list[str], context: object, *, cwd: Path | str | None = None,
         env: dict[str, str] | None = None, extra_ro: tuple[Path, ...] = (),
         extra_rw: tuple[Path, ...] = (), ignore_sigint: bool = False) -> list[str]:
    """``argv`` to run in the jail when there is one, else ``argv`` itself.
    ``env`` is the environment the command gets; its PATH decides which PATH
    directories are bound back. ``context`` is the ToolContext whose
    `jail.bind` applies."""
    jail = ACTIVE
    if jail is None:
        return list(argv)
    path_value = (env if env is not None else os.environ).get("PATH", "")
    return jail.argv(list(argv), cwd=cwd, env_path=path_value,
                     setting=getattr(context, "jail_bind", ()),
                     extra_ro=extra_ro, extra_rw=extra_rw, ignore_sigint=ignore_sigint)


def private_root() -> Path:
    return paths.state_root() / "jail"


def _clear_stale_private_dirs() -> None:
    """Remove the private directories of js processes that are gone."""
    root = private_root()
    if not root.is_dir():
        return
    for entry in root.iterdir():
        pid_text = entry.name.split("-", 1)[0]
        if not pid_text.isdigit():
            continue
        try:
            os.kill(int(pid_text), 0)
        except ProcessLookupError:
            shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def enter(directory: Path | str) -> Jail:
    """Put this process's tools in a jail at ``directory``. Raises JailError
    with one line when it cannot: no bwrap, a bad DIR, a failing self-test."""
    global ACTIVE
    root = _real(Path(directory).expanduser())
    if not root.is_dir():
        raise JailError(f"-C target is not a directory: {directory}")
    if root == Path("/"):
        raise JailError("-C / is not a jail; name a directory")
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise JailError("-C runs tools under bubblewrap, and bwrap is not on PATH; install bubblewrap")
    _clear_stale_private_dirs()
    private_root().mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=private_root()))
    jail = Jail(root=root, bwrap=bwrap, private=private)
    jail.tmp.mkdir()
    jail.js_tmp.mkdir()
    failure = jail.self_test()
    if failure is not None:
        jail.cleanup()
        raise JailError(f"-C: bubblewrap cannot start a jail: {failure}")
    leave()
    ACTIVE = jail
    os.environ["JS_JAIL"] = str(root)
    atexit.register(jail.cleanup)
    return jail


def leave() -> None:
    """Take this process's tools out of the jail."""
    global ACTIVE
    if ACTIVE is not None:
        ACTIVE.cleanup()
    ACTIVE = None
    os.environ.pop("JS_JAIL", None)
