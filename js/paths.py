"""Every location js keeps outside a project, and nothing else.

One home: `--home DIR`, else `$JS_HOME`, else `~/.js/`. Startup resolves it
once to an absolute directory. Project `.js/` directories (jsrc, jsrc.local,
agents, skills, commands, toolbox, JS.md) stay with their project: each one
from the project root (`project_root`) down to the working directory is a
layer over the home, the nearest winning. A `.isolate` marker is a border: no
layer above it is read, the home included (`isolation`).

`ensure_home()` creates every directory of the layout on each start, so the
whole structure is always there:

    ~/.js/
      jsrc           operator overrides; /save writes session changes
      JS.md  tools.yaml  keys   files; not created, only read
      agents/  skills/  toolbox/
      logins/        credentials and the model-list cache
      sessions/      every session, filed by its project (`project_namespace`)
      state/         prompt history, undo store, kernel output, spilled results,
                     commit backups
      logs/          logs, transcripts
      cache/         models.dev catalog, env probe cache, session search index
      work/          agent keepers; `:n` notes
      tmp/           agent junk; entries older than a day are cleared on start
      plans/         plan tool output
      probes/        browser screenshots, terminal snapshots

The locations js used before this layout are listed in `legacy_homes()`;
`js.home` moves them here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import agent_files

_ROOT = Path(os.path.realpath(__file__)).parent.parent
_HOME: Path | None = None
_PROJECT_SCOPES = frozenset({"agents", "skills", "commands", "toolbox", "jsrc", "jsrc.local", "tools.yaml"})


def user_home() -> Path:
    return Path.home()


def default_home() -> Path:
    """The operator's conventional profile location."""
    return user_home() / ".js"


def home() -> Path:
    """The resolved invocation profile; direct callers also honor JS_HOME."""
    if _HOME is not None:
        return _HOME
    value = os.environ.get("JS_HOME") or default_home()
    return Path(value).expanduser().resolve(strict=False)


def configure_home(directory: str | Path | None = None) -> Path:
    """Resolve and create the selected profile once at invocation startup."""
    global _HOME
    value = directory if directory is not None else os.environ.get("JS_HOME") or default_home()
    selected = Path(value).expanduser().resolve(strict=False)
    if selected.exists() and not selected.is_dir():
        raise ValueError(f"home is not a directory: {selected}")
    try:
        selected.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"cannot create home {selected}: {exc}") from exc
    _HOME = selected
    os.environ["JS_HOME"] = str(selected)
    return selected


def custom_home() -> bool:
    """Whether this profile is separate from the operator's default profile."""
    return home() != (default_home()).resolve(strict=False)


def _submodule_marker(marker: Path) -> bool:
    """Whether the `.git` file ``marker`` is a submodule's: its gitdir sits in
    another repository's `.git/modules/`. A worktree's `.git` file points into
    `.git/worktrees/` and is a root of its own."""
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    line = next((row for row in text.splitlines() if row.startswith("gitdir:")), "")
    if not line:
        return False
    parts = (marker.parent / line[len("gitdir:"):].strip()).resolve(strict=False).parts
    return any(parts[i:i + 2] == (".git", "modules") for i in range(len(parts) - 1))


def _git_root(current: Path) -> Path | None:
    found: Path | None = None
    for directory in (current, *current.parents):
        marker = directory / ".git"
        if marker.is_dir() or marker.is_file():
            if found is None:
                found = directory
            if marker.is_file() and _submodule_marker(marker):
                continue
            return directory
    return found


def project_root(cwd: Path | None = None) -> Path | None:
    """The top of the project ``cwd`` (default: here) is in: the nearest
    directory at or above it holding a `.git`, lifted out of a submodule to
    the repository that holds it. Outside any repository, an isolation
    border (`isolation`) is a root of its own. None otherwise."""
    current = (cwd or Path.cwd()).resolve(strict=False)
    root = _git_root(current)
    if root is None:
        found = isolation(current)
        root = None if found is None else found.directory
    return root


def project_namespace(cwd: Path | None = None) -> Path:
    """The directory that names the project ``cwd`` (default: here) is in:
    its `project_root`, or ``cwd`` itself outside any project. Sessions are
    filed under it, so every directory of one project shares them."""
    current = (cwd or Path.cwd()).resolve(strict=False)
    root = project_root(current)
    if root == user_home().resolve(strict=False):
        border = isolation(current)
        return border.directory if border is not None else current
    return root or current


def _chain_from(root: Path, current: Path) -> tuple[Path, ...]:
    try:
        rel = current.relative_to(root)
    except ValueError:
        return (root,)
    return (root, *(root.joinpath(*rel.parts[:depth]) for depth in range(1, len(rel.parts) + 1)))


# --isolate (True) / --no-isolate (False) for this run; None follows the markers.
_ISOLATE: bool | None = None

ISOLATE_MARKER = ".isolate"

# Whether git tracks a marker, by its path: asked once per run.
_TRACKED: dict[Path, bool] = {}


def configure_isolate(value: bool | None) -> None:
    """Record this run's --isolate (True) or --no-isolate (False); None
    leaves isolation to the `.isolate` markers."""
    global _ISOLATE
    _ISOLATE = value


def isolate_override() -> bool | None:
    return _ISOLATE


@dataclass(frozen=True)
class Isolation:
    """A border: no setup layer above ``directory`` is read, the home
    included. ``directory``'s `.js/` and every chain layer below it stack over
    stock. ``marker`` is the `.isolate` file that set it, None when
    --isolate did; a marker above the repository root borders at the root."""
    directory: Path
    marker: Path | None

    @property
    def layer(self) -> Path:
        """The topmost setup directory read: ``directory``'s `.js/`."""
        return self.directory / ".js"


def _home_layers() -> set[Path]:
    return {home().resolve(strict=False), default_home().resolve(strict=False)}


def _tracked(marker: Path) -> bool:
    """Whether git tracks ``marker``: a committed `.isolate` is ignored."""
    if marker not in _TRACKED:
        import subprocess
        try:
            result = subprocess.run(["git", "-C", str(marker.parent), "ls-files", "--error-unmatch", "--", marker.name],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            _TRACKED[marker] = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _TRACKED[marker] = False
    return _TRACKED[marker]


def _scanned(current: Path) -> tuple[Path, ...]:
    """The directories a marker is looked for in, nearest first: ``current``
    and its parents below `$HOME`, or every parent outside `$HOME`. The home
    profile and a directory whose `.js/` is the home are left out, since only
    stock is under the home."""
    user = user_home().resolve(strict=False)
    homes = _home_layers()
    found = []
    for directory in (current, *current.parents):
        if directory == user:
            break
        # ``directory`` is resolved, so its `.js` resolves elsewhere only as a symlink.
        layer = directory / ".js"
        if directory in homes or layer in homes or (layer.is_symlink() and layer.resolve(strict=False) in homes):
            continue
        found.append(directory)
    return tuple(found)


def _marker_in(directory: Path) -> Path | None:
    return next((path for path in (directory / ISOLATE_MARKER, directory / ".js" / ISOLATE_MARKER)
                 if path.exists() and not _tracked(path)), None)


def tracked_markers(cwd: Path | None = None) -> tuple[Path, ...]:
    """The `.isolate` files around ``cwd`` that git tracks, which isolation ignores."""
    current = (cwd or Path.cwd()).resolve(strict=False)
    return tuple(path for directory in _scanned(current)
                 for path in (directory / ISOLATE_MARKER, directory / ".js" / ISOLATE_MARKER)
                 if path.exists() and _tracked(path))


def isolation(cwd: Path | None = None) -> Isolation | None:
    """The border a run started in ``cwd`` (default: here) is under, or None.
    The nearest directory at or above ``cwd`` (up to, not including, `$HOME`)
    with an untracked `.isolate` in it or in its `.js/` sets it. Inside a
    repository a marker at or below the root borders there, and one above
    the root borders at the root, since nothing above the root is read.
    Outside a repository the marked directory is the root. --isolate borders
    at the nearest existing project `.js/`, and --no-isolate ignores every
    marker."""
    if _ISOLATE is False:
        return None
    current = (cwd or Path.cwd()).resolve(strict=False)
    root = _git_root(current)
    scanned = _scanned(current)
    if _ISOLATE:
        inside = scanned if root is None else tuple(d for d in scanned if d.is_relative_to(root))
        nearest = next((directory for directory in inside if (directory / ".js").is_dir()), None)
        return None if nearest is None else Isolation(nearest, None)
    for directory in scanned:
        marker = _marker_in(directory)
        if marker is None:
            continue
        if root is not None and not directory.is_relative_to(root):
            return Isolation(root, marker)
        return Isolation(directory, marker)
    return None


def uses_home(cwd: Path | None = None) -> bool:
    """Whether a run started in ``cwd`` reads the home's setup: false under isolation."""
    return isolation(cwd) is None


def project_chain(cwd: Path | None = None) -> tuple[Path, ...]:
    """Every directory from the project root down to ``cwd`` (default: here),
    root first. Empty outside any repository and any border. Under isolation
    it starts at the border directory."""
    current = (cwd or Path.cwd()).resolve(strict=False)
    found = isolation(current)
    if found is not None:
        return _chain_from(found.directory, current)
    root = _git_root(current)
    return () if root is None else _chain_from(root, current)


def project_dirs(cwd: Path | None = None) -> tuple[Path, ...]:
    """Each project `.js/` from the root down to ``cwd``, lowest first, that
    exists. A `.js/` that is the home (or, under another profile, the
    operator's `~/.js`) is the global layer, never a project one."""
    skip = _home_layers()
    found = []
    for directory in project_chain(cwd):
        candidate = directory / ".js"
        if candidate.is_dir() and candidate.resolve(strict=False) not in skip:
            found.append(candidate)
    return tuple(found)


def project_skill_dirs(cwd: Path | None = None) -> tuple[tuple[Path, ...], ...]:
    """Each project directory's skill roots, root first: its cross-client
    `.agents/skills`, then its `.js/skills`. `$HOME/.agents/skills` is the
    shared global root (`shared_skills_dir`), so a chain through `$HOME`
    leaves it out."""
    setup = {directory.parent: directory for directory in project_dirs(cwd)}
    user = user_home().resolve(strict=False)
    layers_found = []
    for directory in project_chain(cwd):
        roots = [] if directory == user else [directory / ".agents" / "skills"]
        if directory in setup:
            roots.append(setup[directory] / "skills")
        if roots:
            layers_found.append(tuple(roots))
    return tuple(layers_found)


def defaults_dir() -> Path:
    """js's stock setup, laid out like ``~/.js``, beside the js package."""
    return _ROOT / "defaults"


def layers(cwd: Path | None = None) -> tuple[Path, ...]:
    """Setup roots, lowest priority first: stock, home, then each project
    `.js/` from the root down to ``cwd``. Project files use their existing scopes."""
    homes = (home(),) if uses_home(cwd) else ()
    return tuple(dict.fromkeys((defaults_dir(), *homes, *project_dirs(cwd))))


def agent_roots(cwd: Path | None = None) -> tuple[Path, ...]:
    """Each layer's agents directory, lowest first: stock, home, then each
    project `.js/agents` from the root down to ``cwd``."""
    homes = (global_agents_dir(),) if uses_home(cwd) else ()
    return (defaults_dir() / "agents", *homes,
            *(directory / "agents" for directory in project_dirs(cwd)))


_AGENT_COMPONENT = re.compile(r"[A-Za-z0-9_-]+")


def validate_agent_id(agent_id: str) -> str:
    if not isinstance(agent_id, str) or not all(_AGENT_COMPONENT.fullmatch(part) for part in agent_id.split(":")):
        from . import messages as msgs

        raise ValueError(msgs.CONFIG_AGENT_ID_MUST_CONTAIN_ONLY_LETTERS_NUMBERS)
    return agent_id


def agent_path(agent_id: str) -> Path:
    """The prompt directory relative to an agent root, e.g. meta/creator."""
    return Path(*validate_agent_id(agent_id).split(":"))


def agent_dirs(agent_id: str, roots: tuple[Path, ...] | None = None) -> tuple[Path, ...]:
    """Agent ``agent_id``'s directory in the highest of ``roots`` (default
    `agent_roots()`) that has one, as a one-entry tuple: a nearer agent
    directory replaces a lower one whole. Every candidate when none has one.
    A directory that cannot be listed is a ValueError, so a lower layer never
    stands in for it."""
    relative = agent_path(agent_id)
    candidates = tuple(dict.fromkeys(root / relative for root in (agent_roots() if roots is None else roots)))
    selected = None
    found_non_agent = False
    for candidate in candidates:
        try:
            if candidate.is_dir():
                entries = tuple(candidate.iterdir())
                if ":" not in agent_id or candidate.is_symlink() or agent_files.has_agent_source(entries):
                    selected = candidate
                else:
                    found_non_agent = True
            elif candidate.is_file():
                found_non_agent = True
        except OSError as exc:
            raise ValueError(f"agent {agent_id!r} at {candidate} is unreadable: {exc}") from exc
    if selected is not None:
        return (selected,)
    if found_non_agent:
        raise ValueError(f"unknown agent {agent_id!r}; searched " + searched(roots=candidates))
    return candidates


def walk_agent_dirs(root: Path, *, on_error: Callable[[OSError], None] | None = None) -> Iterator[tuple[str, Path]]:
    """Discover flat agents and nested agents with source files. Directory
    symlinks are leaves, so discovery never wakes a linked mount or follows a cycle."""
    def visit(directory: Path, prefix: str) -> Iterator[tuple[str, Path]]:
        try:
            entries = sorted(directory.iterdir())
            if ":" in prefix and agent_files.has_agent_source(entries):
                yield prefix, directory
        except OSError as exc:
            if on_error:
                on_error(exc)
            return
        for entry in entries:
            if not _AGENT_COMPONENT.fullmatch(entry.name):
                continue
            agent_id = f"{prefix}:{entry.name}" if prefix else entry.name
            try:
                if entry.is_symlink():
                    yield agent_id, entry
                elif entry.is_dir():
                    if not prefix:
                        yield agent_id, entry
                    yield from visit(entry, agent_id)
            except OSError as exc:
                if on_error:
                    on_error(exc)

    if root.is_dir():
        yield from visit(root, "")


def agent_names(roots: tuple[Path, ...] | None = None) -> list[str]:
    """Sorted qualified agent names across the selected roots."""
    return sorted({name for root in (agent_roots() if roots is None else roots)
                   for name, directory in walk_agent_dirs(root) if not directory.is_symlink() or directory.is_dir()})


def _file_layers(rel: str | Path) -> tuple[Path, ...]:
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"setup path must be relative: {rel}")
    roots = layers()
    if path.parts and path.parts[0] in _PROJECT_SCOPES:
        return roots
    return roots[:2] if uses_home() else roots[:1]


def searched(rel: str | Path = "", roots: Iterable[Path] | None = None) -> str:
    """What a failed lookup read, for its error message: ``rel`` under each of
    ``roots`` (default: every layer that may hold ``rel``), lowest first,
    comma-separated."""
    return ", ".join(str(root / rel) for root in (_file_layers(rel) if roots is None else roots))


# `find` of a path outside the project scopes, by the path, the stock and home
# directories, the start directory, --isolate and the modification time of the
# home directory that would hold it. The stock copy never changes under a run,
# so a file the home gains or loses changes the key.
_FOUND: dict[tuple, Path | None] = {}


# What `find` makes of each ``rel`` it was given as a string: its normal
# spelling, and whether it lies outside the project scopes.
_RELS: dict[str, tuple[str, bool]] = {}


def _rel(rel: str | Path) -> tuple[str, bool]:
    text = os.fspath(rel)
    known = _RELS.get(text)
    if known is None:
        path = Path(text)
        known = (str(path), bool(path.parts) and path.parts[0] not in _PROJECT_SCOPES and not path.is_absolute())
        if len(_RELS) >= 256:
            _RELS.clear()
        _RELS[text] = known
    return known


def find(rel: str | Path) -> Path | None:
    """The highest layer's file at ``rel``, including a zero-byte file."""
    text, outside_project = _rel(rel)
    if outside_project:
        selected = str(home())
        try:
            changed = os.stat(os.path.dirname(os.path.join(selected, text))).st_mtime_ns
        except OSError:
            changed = None
        key = (text, str(defaults_dir()), selected, os.getcwd(), _ISOLATE, changed)
        # Read once: another thread may clear the cache between a store and a lookup.
        try:
            return _FOUND[key]
        except KeyError:
            pass
        path = Path(text)
        found = next((root / path for root in reversed(_file_layers(path)) if (root / path).is_file()), None)
        if len(_FOUND) >= 256:
            _FOUND.clear()
        _FOUND[key] = found
        return found
    return next((root / rel for root in reversed(_file_layers(rel)) if (root / rel).is_file()), None)


def names(rel_dir: str | Path) -> list[str]:
    """Sorted union of the immediate names in a setup directory."""
    return sorted({entry.name for root in _file_layers(rel_dir)
                   if (root / rel_dir).is_dir() for entry in (root / rel_dir).iterdir()})


def global_config_file() -> Path:
    return home() / "jsrc"


def save_config_file(cwd: Path | None = None) -> Path:
    """The jsrc `/save` writes: the home's, or under isolation the border
    layer's, the topmost layer the run reads."""
    found = isolation(cwd)
    return global_config_file() if found is None else found.layer / "jsrc"


def legacy_global_config_file() -> Path:
    """Pre-jsrc TOML config path, kept only for `js --migrate-config`."""
    return home() / "config.toml"


def global_agents_dir() -> Path:
    return home() / "agents"


def global_skills_dir() -> Path:
    return home() / "skills"


def shared_skills_dir() -> Path:
    """Cross-client skills: ~/.agents/skills, isolated inside a custom profile."""
    if custom_home():
        return home() / ".agents" / "skills"
    return user_home() / ".agents" / "skills"


def global_commands_dir() -> Path:
    """`~/.js/commands`: each NAME.md is the command /NAME."""
    return home() / "commands"


def global_toolbox_dir() -> Path:
    return home() / "toolbox"


def tools_config_file() -> Path:
    return home() / "tools.yaml"


def global_env_file() -> Path:
    return home() / ".env"


def global_instruction_files() -> tuple[Path, Path]:
    """js's own always-on operator context, loaded whatever directory js runs in.

    Named JS.md, deliberately NOT AGENTS.md. AGENTS.md is the per-repo convention a
    dozen other tools also read, so a global one would silently apply repo-shaped
    instructions everywhere. ~/.js/AGENTS.md loads only the way any other
    directory's does: by running js from inside ~/.js, where it is the project
    file for that directory.
    """
    root = home()
    return root / "JS.md", root / "JS.local.md"


def login_store_dir() -> Path:
    return home() / "logins"


def sessions_root() -> Path:
    return home() / "sessions"


def state_root() -> Path:
    return home() / "state"


def kernel_state_root() -> Path:
    """Per-kernel artifact directories: rich output images and kernel.log."""
    return state_root() / "kernel"


def tool_results_dir() -> Path:
    """Tool results too large to inline, spilled whole for byte-range reads."""
    return state_root() / "tool-results"


def media_dir() -> Path:
    """The image and media bytes sessions send, one file per sha256 (`js.media`)."""
    return state_root() / "media"


def commit_backups_dir() -> Path:
    return state_root() / "commit-backups"


def prompt_history_file() -> Path:
    """Every prompt sent at a REPL prompt, from every run (`js.prompt_history`)."""
    return state_root() / "history.jsonl"


def keys_file() -> Path:
    """The key bindings file (`js.keys`)."""
    return home() / "keys"


def home_migration_marker() -> Path:
    """Present once the automatic first-run migration has run."""
    return state_root() / "home-migrated"


def logs_root() -> Path:
    return home() / "logs"


def transcript_root() -> Path:
    return logs_root() / "transcript"


def cache_root() -> Path:
    return home() / "cache"


def model_catalog_dir() -> Path:
    return cache_root() / "modelsdotdev"


def model_catalog_db_path() -> Path:
    return model_catalog_dir() / "modelsdotdev.sqlite"


def model_catalog_status_path() -> Path:
    return model_catalog_dir() / "status.json"


def work_dir() -> Path:
    return home() / "work"


def notes_dir() -> Path:
    """Where `:n` notes and `:w` buffer saves go."""
    return work_dir() / "notes"


def tmp_dir() -> Path:
    """js's scratch directory, created on demand."""
    path = home() / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def plans_dir() -> Path:
    return home() / "plans"


def probes_dir() -> Path:
    return home() / "probes"


def browser_probes_dir() -> Path:
    return probes_dir() / "browser"


def terminal_snapshots_dir() -> Path:
    return probes_dir() / "terminal"


def layout_dirs() -> tuple[Path, ...]:
    """The directories `ensure_home()` creates, in the order the layout lists them."""
    return (
        global_agents_dir(),
        global_skills_dir(),
        global_toolbox_dir(),
        login_store_dir(),
        sessions_root(),
        state_root(),
        logs_root(),
        cache_root(),
        work_dir(),
        home() / "tmp",
        plans_dir(),
        probes_dir(),
    )


def ensure_home() -> None:
    """Create every directory of the layout that is missing."""
    for directory in layout_dirs():
        directory.mkdir(parents=True, exist_ok=True)


def _xdg_base(variable: str, default: Path) -> Path:
    value = os.environ.get(variable, "").strip()
    return Path(value) if value else user_home() / default


def legacy_homes() -> dict[str, Path]:
    """The pre-`~/.js` locations the home migration reads, by name.

    config and data are where js kept them: the XDG config and data homes
    (their defaults when the variables are unset), each with a `js` directory.
    """
    return {
        "config": _xdg_base("XDG_CONFIG_HOME", Path(".config")) / "js",
        "data": _xdg_base("XDG_DATA_HOME", Path(".local") / "share") / "js",
        "inbox": user_home() / "inbox" / "agents" / "js",
    }


def file_stamp(path: str | os.PathLike[str]) -> tuple[int, int] | None:
    """A file's size and modification time, the pair a cache of what was read
    from it is keyed by; None when it cannot be statted."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    return info.st_size, info.st_mtime_ns
