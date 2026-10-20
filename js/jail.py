"""`js -C DIR`: the jail that keeps the agent's tools inside DIR.

The jail keeps the agent's context clean. It is not a defence against a
hostile model. Under `-C`:

- every command a tool starts (shell, kernel, terminal, wiki converters) runs
  under bubblewrap. The system is read-only. `/home` and the operator's home
  are empty tmpfs mounts, and so are `/run/user` and every network
  filesystem mount. The jail's `/tmp` and `~/.js/tmp` are directories private
  to this js process. DIR is bound read-write at its real path. The PATH
  directories, rustup's toolchains and the kernel's interpreter under a hidden
  tree or the host's `/tmp`, the `JAIL_BIND` entries and the `/add` binds are bound back.
  Each `JAIL_HIDE` path (~/.js, ~/.ssh and the like) those binds would show
  is mounted empty, so a jail rooted at $HOME still shows no keys. The
  network is shared.
- the file tools resolve every path and refuse one outside DIR and the bound
  paths (`confine`). Their results name the jail's /tmp and ~/.js/tmp as the
  jail shows them, not the private directory behind them (`shown`).

The jail is process-wide: subagents run in this process and use the same one.
The operator's `exec` commands (js/hookexec.py) are not tools and run on the
host.
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
from collections.abc import Callable
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
    """The `JAIL_BIND` setting's value as binds. Raises ValueError."""
    if not isinstance(specs, (list, tuple)):
        raise ValueError("JAIL_BIND is a JSON list of \"path[:rw]\" strings")
    return [parse_bind(item) for item in specs]


def parse_hide(spec: str) -> Path:
    """One `JAIL_HIDE` entry: a path, absolute or starting with ``~``.
    Raises ValueError."""
    text = str(spec).strip()
    path = Path(os.path.expanduser(text)) if text else Path()
    if not text or not path.is_absolute():
        raise ValueError(f"hide path must be absolute or start with ~: {spec!r}")
    return Path(os.path.abspath(path))


def parse_hides(specs) -> list[Path]:
    """The `JAIL_HIDE` setting's value as paths. Raises ValueError."""
    if not isinstance(specs, (list, tuple)):
        raise ValueError("JAIL_HIDE is a JSON list of path strings")
    return [parse_hide(item) for item in specs]


def _default_hides() -> tuple[str, ...]:
    from . import settings

    return tuple(settings.default_value("JAIL_HIDE") or ())


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


def _toolchain_homes(home: Path) -> list[Path]:
    """Trees a PATH shim reads its toolchains from: rustup's ``cargo`` and
    ``rustc`` in /usr/bin find theirs under RUSTUP_HOME, ~/.rustup by default."""
    rustup = Path(os.environ.get("RUSTUP_HOME") or home / ".rustup")
    return [_real(rustup)] if rustup.is_dir() else []


def _is_dir(path: Path) -> bool:
    return path.is_dir()


def hidden_roots() -> list[Path]:
    """The trees the jail replaces with an empty tmpfs, outermost first:
    /home, the operator's home, /run/user, every network filesystem, and every
    other mount that shows one of those.

    A mount point is taken from the mount table as it is, never statted: a
    dead network mount would block the stat, and with it every jailed
    command. A path inside a tree already in the list is not statted either."""
    mounts = _mounts()
    points = {m.point for m in mounts}
    base = [Path("/home"), *operator_homes(), Path("/run/user"),
            *(m.point for m in mounts if m.fstype in NETWORK_FS)]
    candidates = [*base, *(view for tree in base for view in _other_views(tree, mounts))]
    roots: list[Path] = []
    for candidate in sorted(dict.fromkeys(candidates), key=lambda p: len(p.parts)):
        if candidate == Path("/") or any(_under(candidate, root) for root in roots):
            continue
        if candidate not in points and not _is_dir(candidate):
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
        """The `JAIL_BIND` entries plus the `/add` binds. Invalid entries in
        ``setting`` are skipped; `/set` refuses them."""
        out: list[Bind] = []
        for item in setting or ():
            with contextlib.suppress(ValueError):
                out.append(parse_bind(item))
        return [*out, *self.added]

    def hides(self, hide: object = None) -> list[Path]:
        """The `JAIL_HIDE` paths as the host resolves them; None is the
        `js/jsrc` list. Invalid entries are skipped; `/set` refuses them."""
        out = [_real(path) for path in self._hide_entries(hide)]
        # The same directory where another mount shows it (a home on a btrfs
        # subvolume, a bind mount elsewhere) is hidden there too.
        mounts = _mounts()
        out += [view for path in list(out) for view in _other_views(path, mounts)]
        return list(dict.fromkeys(out))

    def _hide_entries(self, hide: object) -> list[Path]:
        """The valid `JAIL_HIDE` entries as written; None is the `js/jsrc` list."""
        out: list[Path] = []
        for item in _default_hides() if hide is None else hide or ():
            with contextlib.suppress(ValueError):
                out.append(parse_hide(item))
        return out

    def hidden_test(self, hide: object = None) -> Callable[[Path], bool]:
        """`hidden` with the `JAIL_HIDE` paths read once, for a tool that
        asks about every entry of a walk."""
        own = [_real(self.private), _real(paths.tool_results_dir())]
        named = [_real(entry.parent) / entry.name for entry in self._hide_entries(hide)]
        hidden = [*self.hides(hide), *named]
        return lambda real: (any(_under(real, h) for h in hidden)
                             and not any(_under(real, area) for area in own))

    def hidden(self, real: Path, hide: object = None) -> bool:
        """Whether host path ``real`` lies in a `JAIL_HIDE` path, outside js's
        own directories there (the private dir, spilled tool results). An
        entry that is a symlink is hidden by its own name too, for a tool
        that does not follow the last component (remove)."""
        return self.hidden_test(hide)(real)

    def hidden_under(self, root: Path, hide: object = None) -> list[Path]:
        """The `JAIL_HIDE` paths inside host directory ``root``, for a tool
        that walks it on the host."""
        real_root = _real(root)
        return [h for h in self.hides(hide) if h != real_root and _under(h, real_root)]

    def _areas(self, setting: object) -> list[Bind]:
        """Every tree a file tool may reach, as the host sees it."""
        explicit = [Bind(self.root, True), *self.binds(setting)]
        areas = [Bind(self.root, True), Bind(self.private, True)]
        areas += [Bind(_real(b.path), b.rw) for b in self.binds(setting)]
        areas.append(Bind(_real(paths.tool_results_dir()), False))
        # A PATH directory inside DIR or a bind is not bound back read-only by
        # `argv` (its `covered` check), so it keeps that bind's access here too.
        areas += [Bind(p, False) for p in self._path_dirs(os.environ.get("PATH", ""))
                  if not any(_under(p, _real(b.path)) for b in explicit)]
        return areas

    def _path_dirs(self, path_value: str) -> list[Path]:
        """PATH directories that sit in a hidden tree or under the host /tmp
        (the ones `argv` binds back), resolved."""
        hidden = hidden_roots()
        out: list[Path] = []
        for entry in path_value.split(os.pathsep):
            if not entry or not os.path.isabs(entry):
                continue
            real = _real(Path(entry))
            if real.is_dir() and (any(_under(real, h) for h in hidden) or Path("/tmp") in real.parents):
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
        # `argv` binds the private ~/.js/tmp last, over DIR and every bind.
        js_tmp = paths.user_home() / ".js" / "tmp"
        if _under(path, js_tmp):
            return self.js_tmp / path.relative_to(js_tmp)
        if any(_under(path, b.path) or _under(path, _real(b.path))
               for b in [Bind(self.root), *self.binds(setting)]):
            return path
        # A hidden tree stays hidden even where it sits under /tmp.
        if any(_under(path, root) for root in hidden_roots()):
            return path
        if _under(path, Path("/tmp")):
            # A PATH directory under the host /tmp is bound over the private one.
            if any(_under(_real(path), d) for d in self._path_dirs(os.environ.get("PATH", ""))):
                return path
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
                setting: object = (), hide: object = None) -> Path:
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
        if self.hidden(real, hide):
            raise JailError(f"{path} is hidden in the jail (JAIL_HIDE)")
        # Removing or moving a tree takes everything under it, hidden or not.
        if write and any(h != real and _under(h, real) for h in self.hides(hide)):
            raise JailError(f"{path} holds a path hidden in the jail (JAIL_HIDE); a tool may not change it")
        if write and not best.rw:
            raise JailError(f"{path} is read-only in the jail (bound from {best.path})")
        return real

    def bound(self, path: Path, setting: object = ()) -> bool:
        """Whether ``path`` lies in DIR, a `JAIL_BIND` entry or an /add bind."""
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
             extra_rw: tuple[Path, ...] = (), ignore_sigint: bool = False,
             hide: object = None) -> list[str]:
        """``argv`` wrapped to run in the jail.

        ``env_path`` is the PATH the command runs with; its directories under a
        hidden tree or the host /tmp are bound back read-only. ``extra_ro``/``extra_rw`` bind
        more paths for this one command (the kernel's interpreter, its sockets).
        Each ``hide`` path (`JAIL_HIDE`) a bind would show is mounted empty
        over it; what js binds inside one is bound again on top."""
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
        implicit += [Bind(p, False) for p in _toolchain_homes(home)]
        implicit += [Bind(p, False) for extra in extra_ro for p in reach(Path(extra))]
        # What a JAIL_HIDE path may not cover: js's own binds (spilled tool
        # results, this command's kernel paths), never DIR, a JAIL_BIND or
        # /add entry, or a PATH directory.
        candidates = [Bind(_real(paths.tool_results_dir()), False),
                      *(Bind(p, False) for extra in extra_ro for p in reach(Path(extra))),
                      *(Bind(Path(p), True) for p in extra_rw)]
        # The host /tmp is replaced by the private one, so a path under it is
        # as unreachable as one in a hidden tree and is bound back. A replaced
        # root itself on PATH is not bound back: it would cover its replacement.
        replaced = [*hidden, Path("/tmp")]
        implicit = [b for b in implicit
                    if any(_under(b.path, h) and b.path != h for h in replaced)
                    and not covered(b.path)]
        # Outer paths first, so a bind inside another lands on top of it.
        binds = sorted(dict.fromkeys([*implicit, *explicit]), key=lambda b: len(b.path.parts))
        for bind in binds:
            src = str(bind.path)
            out += ["--bind-try" if bind.rw else "--ro-bind-try", src, src]
        out += self._hide_args(hide, binds, candidates, [*hidden, Path("/tmp")])
        # The private ~/.js/tmp goes on top of DIR, the binds and the blanks.
        out += ["--bind", str(self.js_tmp), str(home / ".js" / "tmp")]
        if cwd is not None:
            out += ["--chdir", str(cwd)]
        prefix = _IGNORE_SIGINT if ignore_sigint else []
        return [*prefix, *out, "--", *argv]

    def _hide_args(self, hide: object, binds: list[Bind], candidates: list[Bind],
                   replaced: list[Path]) -> list[str]:
        """bwrap arguments that blank each `JAIL_HIDE` path the jail would
        show: one outside the ``replaced`` trees (it shows through the
        read-only /), or one a bind reaches from above or from inside. Then
        bind again the jail's private directory and the ``candidates`` that
        lie inside a blanked path."""
        shown = [h for h in self.hides(hide)
                 if os.path.lexists(h)
                 and (not any(_under(h, tree) for tree in replaced)
                      or any(_under(h, _real(b.path)) or _under(_real(b.path), h) for b in binds))]
        # A bind reached through a symlink (JAIL_BIND ~/alias -> ~/.ssh) is
        # mounted at the link's path, not the target's: blank it there too.
        targets: dict[Path, Path] = {h: h for h in shown}
        for bind in binds:
            at, real_at = Path(os.path.abspath(bind.path)), _real(bind.path)
            if at == real_at:
                continue
            for hidden in self.hides(hide):
                if not os.path.lexists(hidden):
                    continue
                if _under(real_at, hidden):
                    targets.setdefault(at, hidden)
                elif _under(hidden, real_at):
                    targets.setdefault(at / hidden.relative_to(real_at), hidden)
        out: list[str] = []
        # A hidden file shows as an empty file (a bound /dev/null would be a
        # device on a nodev mount and refuse to open).
        empty = self.private / "empty"
        if targets and not empty.exists():
            empty.touch(mode=0o444)
        for target in sorted(targets, key=lambda p: len(p.parts)):
            out += (["--tmpfs", str(target)] if _real(target).is_dir()
                    else ["--ro-bind", str(empty), str(target)])

        def inside(path: Path) -> bool:
            return any(path != h and _under(path, h) for h in shown)

        again: list[str] = []
        if inside(_real(self.private)):
            again += ["--bind", str(self.private), str(self.private)]
        for bind in sorted(dict.fromkeys(candidates), key=lambda b: len(b.path.parts)):
            if inside(_real(bind.path)):
                src = str(bind.path)
                again += ["--bind-try" if bind.rw else "--ro-bind-try", src, src]
        return out + again

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


def shape(source: object) -> tuple:
    """The settings a long-running jailed process (a kernel, a terminal
    session) is built from, as its record or a tool context holds them:
    JAIL_BIND, JAIL_HIDE and the kept environment. A process started under
    another shape than the context's now must not run anything more. Without
    a jail there is nothing they shape."""
    if ACTIVE is None:
        return ()
    hide = getattr(source, "jail_hide", None)
    return (tuple(getattr(source, "jail_bind", ()) or ()),
            tuple(hide) if hide is not None else None,
            tuple(getattr(source, "shell_env_allow", None) or getattr(source, "env_allow", ()) or ()))


def shown(text: str) -> str:
    """``text`` as `Jail.shown` writes it under a jail, else itself."""
    return text if ACTIVE is None else ACTIVE.shown(text)


def hider(context: object) -> Callable[[Path], bool]:
    """Under a jail, a test of whether a path, with its symlinks resolved,
    lies in a `JAIL_HIDE` path of ``context``; the paths are read once."""
    if ACTIVE is None:
        return lambda path: False
    test = ACTIVE.hidden_test(getattr(context, "jail_hide", None))
    return lambda path: test(_real(Path(path)))


def hidden_under(root: Path, context: object) -> list[Path]:
    """Under a jail, the `JAIL_HIDE` paths of ``context`` inside host
    directory ``root``: what a tool walking ``root`` on the host leaves out."""
    if ACTIVE is None:
        return []
    return ACTIVE.hidden_under(root, getattr(context, "jail_hide", None))


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
    `JAIL_BIND` and `JAIL_HIDE` apply."""
    jail = ACTIVE
    if jail is None:
        return list(argv)
    path_value = (env if env is not None else os.environ).get("PATH", "")
    return jail.argv(list(argv), cwd=cwd, env_path=path_value,
                     setting=getattr(context, "jail_bind", ()),
                     extra_ro=extra_ro, extra_rw=extra_rw, ignore_sigint=ignore_sigint,
                     hide=getattr(context, "jail_hide", None))


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
