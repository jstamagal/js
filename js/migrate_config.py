"""`js --migrate-config`: convert a legacy config.toml into a jsrc set-script."""

from __future__ import annotations

import json

from . import (
    messages as msgs,
    paths as _paths,
    rename,
    settings,
)


def _flatten_toml(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten a nested TOML dict into (dotted_key, scalar_str) set-line pairs.
    Tables recurse; lists / arrays-of-tables become a JSON value."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.extend(_flatten_toml(value, prefix=f"{dotted}."))
        elif isinstance(value, bool):
            out.append((dotted, "on" if value else "off"))
        elif isinstance(value, (list, tuple)):
            out.append((dotted, json.dumps(value)))
        else:
            out.append((dotted, str(value)))
    return out


def run_migrate_config() -> int:
    """One-shot: convert a legacy config.toml into a jsrc set-script, then exit."""
    import tomllib

    legacy = _paths.legacy_global_config_file()
    target = _paths.global_config_file()
    if not legacy.exists():
        msgs.warn(msgs.MIGRATE_NO_LEGACY, path=legacy)
        return 1
    if target.exists():
        msgs.warn(msgs.MIGRATE_TARGET_EXISTS, path=target)
        return 1
    with legacy.open("rb") as fp:
        data = tomllib.load(fp)
    lines = ["# migrated from config.toml by `js --migrate-config`", ""]
    deleted = []
    for key, value in _flatten_toml(data):
        if rename.is_deleted(key):
            deleted.append(key)
            lines.append(f"# deleted: set {key} {value}")
        else:
            new = rename.renamed_key(key) or key
            verb = "fset" if rename.is_fset(new) else "set"
            lines.append(f"{verb} {new} {settings.jsrc_text(new, rename.renamed_value(key, str(value)))}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    msgs.say(msgs.MIGRATE_WROTE, path=target, legacy=legacy)
    for key in deleted:
        msgs.warn(msgs.MIGRATE_DELETED, key=key)
    return 1 if deleted else 0

