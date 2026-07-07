"""Canonical filesystem locations for js.

Config lives in the platform config directory; runtime/session state lives in
the platform data directory.  Project-local `.js/` files remain project-local
by design.
"""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_config_path, user_data_path

APP_NAME = "js"


def config_dir() -> Path:
    return Path(user_config_path(APP_NAME, appauthor=False))


def data_dir() -> Path:
    return Path(user_data_path(APP_NAME, appauthor=False))


def global_config_file() -> Path:
    return config_dir() / "jsrc"


def legacy_global_config_file() -> Path:
    """Pre-jsrc TOML config path, kept only for `js --migrate-config`."""
    return config_dir() / "config.toml"


def global_agents_dir() -> Path:
    return config_dir() / "agents"


def global_skills_dir() -> Path:
    return config_dir() / "skills"


def global_agents_files() -> tuple[Path, Path]:
    root = config_dir()
    return root / "AGENTS.md", root / "AGENTS.local.md"


def sessions_root() -> Path:
    return data_dir() / "sessions"


def state_root() -> Path:
    return data_dir() / "state"


def logs_root() -> Path:
    return data_dir() / "logs"


def transcript_root() -> Path:
    return data_dir() / "transcript"


def login_store_dir() -> Path:
    return config_dir()


def model_catalog_dir() -> Path:
    return data_dir() / "modelsdotdev"


def model_catalog_db_path() -> Path:
    return model_catalog_dir() / "modelsdotdev.sqlite"


def model_catalog_status_path() -> Path:
    return model_catalog_dir() / "status.json"
