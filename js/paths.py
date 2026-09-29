"""Every location js keeps outside a project, and nothing else.

One home: `~/.js/`. Project-local `.js/` files (jsrc, jsrc.local, agents,
skills, toolbox) stay with the project and are not named here.

    ~/.js/
      jsrc              the user layer (what /save writes)
      agents/  skills/  toolbox/
      logins/           credential store and its model-list cache
      sessions/         session JSONL, per agent
      state/            undo store, debug logs, kernel artifacts, spilled results
      logs/             autolog, transcript, compaction flights
      cache/            modelsdotdev catalog
      work/             what the agent must not lose; `:n` notes
      tmp/              js's scratch; stale entries are cleared on start
      plans/            the plan tool
      probes/           browser probes and terminal snapshots

The locations js used before this layout are listed in `legacy_homes()`;
`js.home_migration` moves them here.
"""

from __future__ import annotations

import os
from pathlib import Path


def user_home() -> Path:
    return Path.home()


def home() -> Path:
    return user_home() / ".js"


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
    """`~/.agents/skills`, the skill root other agent tools read too."""
    return user_home() / ".agents" / "skills"


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


def commit_backups_dir() -> Path:
    return state_root() / "commit-backups"


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
