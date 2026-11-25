"""Every location js keeps outside a project, and nothing else.

One home: `--home DIR`, else `$JS_HOME`, else `~/.js/`. Startup resolves it
once to an absolute directory. Project `.js/` directories (jsrc, jsrc.local,
agents, skills, commands, toolbox, JS.md) stay with their project: each one
from the project root (`project_root`) down to the working directory is a
layer over the home, the nearest winning.

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
from collections.abc import Iterable
from pathlib import Path

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


def project_root(cwd: Path | None = None) -> Path | None:
    """The top of the project ``cwd`` (default: here) is in: the nearest
    directory at or above it holding a `.git`, lifted out of a submodule to
    the repository that holds it. None outside any repository."""
    current = (cwd or Path.cwd()).resolve(strict=False)
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


def project_namespace(cwd: Path | None = None) -> Path:
    """The directory that names the project ``cwd`` (default: here) is in:
    its `project_root`, or ``cwd`` itself outside any project. Sessions are
    filed under it, so every directory of one project shares them."""
    current = (cwd or Path.cwd()).resolve(strict=False)
    return project_root(current) or current


def project_chain(cwd: Path | None = None) -> tuple[Path, ...]:
    """Every directory from the project root down to ``cwd`` (default: here),
    root first. Empty outside any repository."""
    current = (cwd or Path.cwd()).resolve(strict=False)
    root = project_root(current)
    if root is None:
        return ()
    try:
        rel = current.relative_to(root)
    except ValueError:
        return (root,)
    return (root, *(root.joinpath(*rel.parts[:depth]) for depth in range(1, len(rel.parts) + 1)))


def project_dirs(cwd: Path | None = None) -> tuple[Path, ...]:
    """Each project `.js/` from the root down to ``cwd``, lowest first, that
    exists. A `.js/` that is the home (or, under another profile, the
    operator's `~/.js`) is the global layer, never a project one."""
    skip = {home().resolve(strict=False), default_home().resolve(strict=False)}
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
    return tuple(dict.fromkeys((defaults_dir(), home(), *project_dirs(cwd))))


def agent_roots(cwd: Path | None = None) -> tuple[Path, ...]:
    """Each layer's agents directory, lowest first: stock, home, then each
    project `.js/agents` from the root down to ``cwd``."""
    return (defaults_dir() / "agents", global_agents_dir(),
            *(directory / "agents" for directory in project_dirs(cwd)))


def agent_dirs(agent_id: str, roots: tuple[Path, ...] | None = None) -> tuple[Path, ...]:
    """Agent ``agent_id``'s directory in the highest of ``roots`` (default
    `agent_roots()`) that has one, as a one-entry tuple: a nearer agent
    directory replaces a lower one whole. Every candidate when none has one.
    A directory that cannot be listed is a ValueError, so a lower layer never
    stands in for it."""
    candidates = tuple(dict.fromkeys(root / agent_id for root in (agent_roots() if roots is None else roots)))
    existing = []
    for candidate in candidates:
        try:
            if candidate.is_dir():
                tuple(candidate.iterdir())
                existing.append(candidate)
        except OSError as exc:
            raise ValueError(f"agent {agent_id!r} at {candidate} is unreadable: {exc}") from exc
    return tuple(existing[-1:]) or candidates


def agent_names(roots: tuple[Path, ...] | None = None) -> list[str]:
    """Sorted names of the agent directories in ``roots`` (default `agent_roots()`)."""
    return sorted({entry.name for root in (agent_roots() if roots is None else roots)
                   if root.is_dir() for entry in root.iterdir() if entry.is_dir()})


def _file_layers(rel: str | Path) -> tuple[Path, ...]:
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"setup path must be relative: {rel}")
    roots = layers()
    return roots if path.parts and path.parts[0] in _PROJECT_SCOPES else roots[:2]


def searched(rel: str | Path = "", roots: Iterable[Path] | None = None) -> str:
    """What a failed lookup read, for its error message: ``rel`` under each of
    ``roots`` (default: every layer that may hold ``rel``), lowest first,
    comma-separated."""
    return ", ".join(str(root / rel) for root in (_file_layers(rel) if roots is None else roots))


def find(rel: str | Path) -> Path | None:
    """The highest layer's file at ``rel``, including a zero-byte file."""
    return next((root / rel for root in reversed(_file_layers(rel)) if (root / rel).is_file()), None)


def names(rel_dir: str | Path) -> list[str]:
    """Sorted union of the immediate names in a setup directory."""
    return sorted({entry.name for root in _file_layers(rel_dir)
                   if (root / rel_dir).is_dir() for entry in (root / rel_dir).iterdir()})


def global_config_file() -> Path:
    return home() / "jsrc"


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
