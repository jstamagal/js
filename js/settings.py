"""TOML settings loader for the js harness.

Loads an ordered list of TOML config files, key-by-key merged with later files
winning, then layered with environment variables and CLI extras on top. The
result is a single flat dict keyed by dotted paths (e.g.
``provider.base_url``) that the rest of the harness reads via the helpers below.

This module owns no provider-era assumptions: it never defaults a base URL or
API key, never sniffs model names, and never writes back to ``os.environ``.
The ``provider`` table exists so the user can opt into an explicit local or
custom endpoint with visible config — one section of one file, no hidden magic.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from typing import Any


# Built-in defaults — every key the harness knows about, with the value used
# when no config file or env var supplies one. Keep this in sync with the
# template written by `write_default_template` on first run.
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_MAX_TOOL_ITERATIONS = 50
DEFAULT_MAX_BASH_OUTPUT_BYTES = 256 * 1024
DEFAULT_MAX_TOOL_RESULT_BYTES = 256 * 1024
DEFAULT_FETCH_TIMEOUT_S = 15
DEFAULT_TRACE = True
DEFAULT_MAX_READ_LINES = 2_000
DEFAULT_MAX_LINE_CHARS = 2_000
DEFAULT_JSONL_MAX_LINE_CHARS = 65536
DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_TASK_MAX_DEPTH = 2
DEFAULT_WIKI_VAULT_LOCK_TIMEOUT_S = 30
DEFAULT_COMPACT_AUTO = True
DEFAULT_COMPACT_CONTEXT_WINDOW = None
DEFAULT_COMPACT_NOTIFY_THRESHOLD = 0.50
DEFAULT_COMPACT_TRIGGER_THRESHOLD = 0.80
DEFAULT_COMPACT_FORCE_THRESHOLD = 0.90
DEFAULT_COMPACT_TAIL_TOKENS = 16384
DEFAULT_COMPACT_MIN_SAVINGS_TOKENS = 400
DEFAULT_COMPACT_CHARS_PER_TOKEN = 4.0
DEFAULT_ARTIFACT_DIR = "/srv/artifacts"
DEFAULT_ARTIFACT_URL = "http://localhost"
DEFAULT_ARTIFACT_BIN = "artifact"


CONFIG_PRECEDENCE_LAYERS = (
    "built-in defaults",
    "platform config.toml",
    "project .js/config.toml",
    "project .js/config.local.toml",
    "env vars",
    "--extra CLI flag",
)
CANONICAL_CONFIG_PRECEDENCE = " < ".join(CONFIG_PRECEDENCE_LAYERS)
TEMPLATE_CONFIG_PRECEDENCE = CANONICAL_CONFIG_PRECEDENCE.replace(
    "platform config.toml", "this file", 1
)


# Knobs the harness currently understands. Each entry's `default` is the value
# when nothing else overrides it. The template writer enumerates this list to
# produce a fully-commented config file on first run.
_KNOWN_KEYS: list[tuple[str, tuple[str, ...], Any, str]] = [
    # (flat key, dotted path, default, comment)
    ("model_id",                  ("model", "id"),                   DEFAULT_MODEL,               "Default model id; unprefixed ids route through AI Gateway."),
    ("max_output_tokens",         ("model", "max_output_tokens"),   None,                        "Per-call max_tokens; unset = models.dev metadata when known, otherwise no explicit cap."),
    ("reasoning_effort",          ("model", "reasoning_effort"),    None,                        "Thinking effort: off|low|medium|high|max."),
    ("max_tool_iterations",       ("limits", "max_tool_iterations"),  DEFAULT_MAX_TOOL_ITERATIONS,  "Max tool calls per turn before the loop gives up."),
    ("max_bash_output_bytes",     ("limits", "max_bash_output_bytes"), DEFAULT_MAX_BASH_OUTPUT_BYTES, "Hard cap on shell stdout per call."),
    ("max_tool_result_bytes",     ("limits", "max_tool_result_bytes"), DEFAULT_MAX_TOOL_RESULT_BYTES, "Hard cap on any tool result string."),
    ("fetch_timeout_s",           ("limits", "fetch_timeout_s"),    DEFAULT_FETCH_TIMEOUT_S,     "fetch() per-request timeout in seconds."),
    ("max_read_lines",            ("limits", "max_read_lines"),     DEFAULT_MAX_READ_LINES,      "Maximum lines returned by read()."),
    ("max_line_chars",            ("limits", "max_line_chars"),     DEFAULT_MAX_LINE_CHARS,      "Maximum characters shown per read/search line."),
    ("jsonl_max_line_chars",      ("limits", "jsonl_max_line_chars"), DEFAULT_JSONL_MAX_LINE_CHARS, "Maximum characters shown per read line for .jsonl files only."),
    ("max_file_bytes",            ("limits", "max_file_bytes"),     DEFAULT_MAX_FILE_BYTES,      "Maximum file bytes read by fs tools."),
    ("task_max_depth",            ("limits", "task_max_depth"),     DEFAULT_TASK_MAX_DEPTH,      "Maximum recursive task/subagent depth."),
    ("wiki_vault_lock_timeout_s", ("limits", "wiki_vault_lock_timeout_s"), DEFAULT_WIKI_VAULT_LOCK_TIMEOUT_S, "Wiki vault lock timeout in seconds."),
    ("debug",                     ("runtime", "debug"),             False,                       "Append per-event records to platform data state/<agent>/debug.log."),
    ("trace",                     ("runtime", "trace"),             DEFAULT_TRACE,               "Pretty-print the tool-call trace line as the model runs."),
    ("provider_id",               ("provider", "id"),               None,                        "Explicit js provider id (e.g. deepseek, openai-codex, ollama)."),
    ("provider_base_url",         ("provider", "base_url"),         None,                        "Explicit provider base URL; leave unset for provider default."),
    ("provider_api_key",          ("provider", "api_key"),          None,                        "Explicit provider API key; leave unset for env/default."),
    ("context_window",            ("compact", "context_window"),   DEFAULT_COMPACT_CONTEXT_WINDOW, "Context window tokens for fullness math; unset = models.dev metadata when known."),
    ("notify_threshold",          ("compact", "notify_threshold"), DEFAULT_COMPACT_NOTIFY_THRESHOLD, "Notify once when context reaches this fraction."),
    ("trigger_threshold",         ("compact", "trigger_threshold"), DEFAULT_COMPACT_TRIGGER_THRESHOLD, "Auto-compact at this fullness fraction."),
    ("force_threshold",           ("compact", "force_threshold"),   DEFAULT_COMPACT_FORCE_THRESHOLD, "Force compact at this fullness fraction."),
    ("tail_tokens",               ("compact", "tail_tokens"),      DEFAULT_COMPACT_TAIL_TOKENS,  "Recent tail budget retained after compaction."),
    ("min_savings_tokens",        ("compact", "min_savings_tokens"), DEFAULT_COMPACT_MIN_SAVINGS_TOKENS, "Skip compaction unless estimated savings exceeds this."),
    ("chars_per_token",           ("compact", "chars_per_token"),  DEFAULT_COMPACT_CHARS_PER_TOKEN, "Fallback/self-calibrating character-to-token estimate."),
    ("pre_hook",                  ("compact", "pre_hook"),         None,                        "Optional shell command whose stdout guides compaction."),
    ("aliases",                   ("wiki", "aliases"),             {},                          "Vault alias map, e.g. creative='/path/to/wiki'."),
    ("artifact_dir",              ("artifact", "dir"),             DEFAULT_ARTIFACT_DIR,        "Artifact library directory."),
    ("artifact_url",              ("artifact", "url"),             DEFAULT_ARTIFACT_URL,        "Artifact HTTP base URL."),
    ("artifact_bin",              ("artifact", "bin"),             DEFAULT_ARTIFACT_BIN,        "Artifact CLI binary."),
]


# Env-var mapping for the env layer. Values are read as strings; consumers
# coerce as needed (int for the *_BYTES / *_S knobs, etc.).
_ENV_OVERRIDES: list[tuple[str, tuple[str, ...], str]] = [
    # (env var name, dotted path, description used for the comments)
    ("JS_MODEL",                 ("model", "id"),                    "env override for [model].id; beats ME_MODEL"),
    ("ME_MODEL",                 ("model", "id"),                    "env alias; overrides config when JS_MODEL is unset"),
    ("JS_MAX_OUTPUT_TOKENS",     ("model", "max_output_tokens"),     "per-call max output tokens"),
    ("JS_REASONING",            ("model", "reasoning_effort"),      "thinking effort: off|low|medium|high|max|xhigh"),
    ("JS_MAX_TOOL_ITERATIONS",   ("limits", "max_tool_iterations"),  "max tool calls per turn"),
    ("JS_MAX_BASH_OUTPUT_BYTES", ("limits", "max_bash_output_bytes"), "shell stdout cap in bytes"),
    ("JS_MAX_TOOL_RESULT_BYTES", ("limits", "max_tool_result_bytes"), "tool result string cap in bytes"),
    ("JS_FETCH_TIMEOUT",         ("limits", "fetch_timeout_s"),       "fetch() timeout in seconds"),
    ("JS_JSONL_MAX_LINE_CHARS",  ("limits", "jsonl_max_line_chars"), "max characters per read line for .jsonl files only"),
    ("JS_DEBUG",                 ("runtime", "debug"),               "set to 1 to enable platform data state/<agent>/debug.log"),
    ("JS_TRACE",                 ("runtime", "trace"),               "0/false to hide the live tool-call trace line"),
    ("JS_PROVIDER",              ("provider", "id"),                  "explicit js provider id"),
    ("JS_BASE_URL",              ("provider", "base_url"),            "explicit provider base URL"),
    ("JS_API_KEY",               ("provider", "api_key"),             "explicit provider API key"),
]
def _coerce_known_value(path: tuple[str, ...], raw: str) -> Any:
    """Coerce an env-var string into the right Python type for ``path``."""
    name = path[-1]
    if name in {"max_output_tokens", "max_tool_iterations", "max_bash_output_bytes",
                "max_tool_result_bytes", "fetch_timeout_s", "max_read_lines", "max_line_chars", "jsonl_max_line_chars", "max_file_bytes", "task_max_depth", "wiki_vault_lock_timeout_s", "context_window", "tail_tokens", "min_savings_tokens"}:
        try:
            return int(raw)
        except ValueError:
            return None
    if name in {"debug", "trace", "auto"}:
        v = raw.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off"}:
            return False
        return None
    return raw


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge ``override`` into ``base`` key-by-key, later wins.

    Tables are merged recursively so a partial ``[provider.extra]`` in a
    later file augments the earlier one instead of replacing it. Non-dict
    values are replaced wholesale.
    """
    for key, value in override.items():
        if (key in base
                and isinstance(base[key], dict)
                and isinstance(value, dict)):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _set_dotted(target: dict, path: tuple[str, ...], value: Any) -> None:
    """Place ``value`` at ``path`` in ``target``, creating dicts as needed."""
    cursor = target
    for part in path[:-1]:
        next_node = cursor.get(part)
        if not isinstance(next_node, dict):
            next_node = {}
            cursor[part] = next_node
        cursor = next_node
    cursor[path[-1]] = value


def _parse_dotted_key(key: str) -> tuple[str, ...]:
    """Split a CLI ``--extra`` argument's left-hand side into a path tuple."""
    parts = tuple(p for p in key.split(".") if p)
    if not parts:
        raise ValueError(f"empty --extra key: {key!r}")
    return parts


def coerce_extra_value(raw: str) -> Any:
    """Coerce a CLI ``--extra KEY=VALUE`` right-hand side.

    Order: int, then float, then true/false/null to bool, else string. The
    bool check is on the lowercased stripped value so ``True`` / ``FALSE``
    behave the same as ``true`` / ``false``.
    """
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none"}:
        return None
    return raw


def parse_extra_arg(arg: str) -> tuple[tuple[str, ...], Any]:
    """Parse one ``--extra KEY=VALUE`` argument into (path, value).

    Splits on the FIRST ``=`` only so values may contain ``=``. Empty key or
    empty value is rejected — the user gets a clear error instead of a silent
    no-op.
    """
    if "=" not in arg:
        raise ValueError(f"--extra expects KEY=VALUE, got: {arg!r}")
    raw_key, raw_value = arg.split("=", 1)
    key = raw_key.strip()
    if not key:
        raise ValueError(f"--extra key is empty: {arg!r}")
    if raw_value == "":
        raise ValueError(f"--extra value is empty: {arg!r}")
    return _parse_dotted_key(key), coerce_extra_value(raw_value)


def load_toml_settings(paths: list[Path]) -> dict:
    """Load an ordered list of TOML files and merge them into one dict.

    Later files win. Missing files are silently skipped. A non-TOML file
    raises — the user wants to know if their config has a syntax error.
    """
    merged: dict = {}
    for path in paths:
        if not path.exists():
            continue
        with path.open("rb") as fp:
            data = tomllib.load(fp)
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top-level must be a TOML table, got {type(data).__name__}")
        _deep_merge(merged, data)
    return merged


def apply_env_overrides(settings: dict, env: dict[str, str] | None = None) -> dict:
    """Overlay process env vars onto ``settings`` (in place).

    JS_* / OPENAI_API_* vars always win over the config file. Returns the same
    dict for chaining. ``ME_MODEL`` is a silent env-layer alias: it overrides
    config files whenever JS_MODEL is unset, and JS_MODEL wins when both exist.
    """
    source = env if env is not None else os.environ
    for var, path, _comment in _ENV_OVERRIDES:
        if var not in source:
            continue
        raw = source[var]
        if var == "ME_MODEL":
            # Silent alias: participate in the env override layer whenever
            # JS_MODEL is unset. JS_MODEL wins when both are present.
            if "JS_MODEL" in source:
                continue
            _set_dotted(settings, path, raw)
            continue
        coerced = _coerce_known_value(path, raw)
        if coerced is None and raw != "":
            # unknown coercion (e.g. non-integer where an int is expected):
            # skip rather than clobber a working value with garbage
            continue
        _set_dotted(settings, path, coerced)
    return settings


def apply_cli_extras(settings: dict, extras: list[str]) -> dict:
    """Apply one-shot ``--extra KEY=VALUE`` arguments on top of env.

    Extras land at any dotted path; the common case is
    ``provider.extra.<key>`` so the same parser can be used for arbitrary
    dotted keys without changing the call site.
    """
    for arg in extras:
        path, value = parse_extra_arg(arg)
        _set_dotted(settings, path, value)
    return settings


def get_dotted(settings: dict, path: tuple[str, ...], default: Any = None) -> Any:
    """Read ``path`` from ``settings`` with a default when any segment is missing."""
    cursor: Any = settings
    for part in path:
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


def collect_settings(
    config_paths: list[Path] | None = None,
    env: dict[str, str] | None = None,
    extras: list[str] | None = None,
) -> dict:
    """Run precedence: built-in defaults < config_paths in order < env < CLI extras.

    ``config_paths`` defaults to the platform config file. Callers such as
    ``js.config.from_env`` pass the global, project, and project-local files
    explicitly. ``env`` defaults to ``os.environ``. ``extras`` are parsed as
    nested dict keyed by dotted paths — read it with ``get_dotted``.
    """
    settings: dict = {}
    for _label, path, default, _comment in _KNOWN_KEYS:
        if default is not None:
            _set_dotted(settings, path, default)

    from . import paths as _paths
    paths = config_paths if config_paths is not None else [_paths.global_config_file()]
    # Build the full env layer against the seeded defaults so ME_MODEL and
    # similar fallbacks can see the current value at the right path. We then
    # apply the file on top, then env, then CLI extras.
    apply_env_overrides(settings, env=env)  # type: ignore[arg-type]
    file_settings = load_toml_settings(paths)
    # Merge file on top of env so file wins over env, but env wins over the
    # seeded default. The order of precedence is default < file < env, which
    # means env is applied LAST to the merged view. We achieve that by
    # re-applying env after the file merge.
    _deep_merge(settings, file_settings)
    apply_env_overrides(settings, env=env)  # type: ignore[arg-type]

    if extras:
        apply_cli_extras(settings, extras)
    return settings


# ---------------------------------------------------------------------------
# First-run template
# ---------------------------------------------------------------------------

def _format_default(default: Any) -> str:
    """Render a default value as a commented-out TOML literal."""
    if default is None:
        return "unset"
    if isinstance(default, str) and default == "":
        return '""  '
    if isinstance(default, bool):
        return str(default).lower()
    if isinstance(default, (int, float)):
        return str(default)
    if isinstance(default, str):
        return f'"{default}"'
    return str(default)


def _template_lines() -> list[str]:
    """Build the commented TOML template the user sees on first run.

    Grouped by section, one line per key with the default value commented out
    so the user can uncomment and edit. ``provider.extra`` is shown as a
    free-form sub-table with the canonical example.
    """
    lines: list[str] = [
        "# js runtime config — generated on first run.",
        "#",
        f"# Precedence, lowest to highest: {TEMPLATE_CONFIG_PRECEDENCE}.",
        "# Every key below is commented out with its default; uncomment and edit.",
        "",
    ]
    by_section: dict[str, list[tuple[tuple[str, ...], Any, str]]] = {}
    for _label, path, default, comment in _KNOWN_KEYS:
        by_section.setdefault(path[0], []).append((path, default, comment))
    section_intro: dict[str, list[str]] = {
        "model":    ["# Default model + per-call model knobs."],
        "limits":   ["# Per-call / per-turn caps. Defaults shown — uncomment to override."],
        "runtime":  ["# Live-runtime toggles."],
        "provider": [
            "# Optional explicit ai-python provider id / base_url / api_key.",
            "# Leave all unset to let ai-python route model ids natively (unprefixed ids go to AI Gateway).",
        ],
        "compact": ["# Cache-first context compaction knobs."],
        "wiki": ["# Wiki vault aliases. Override with [wiki.aliases]."],
        "artifact": ["# Artifact system defaults."],
    }
    for section in ("model", "limits", "runtime", "provider", "compact", "wiki", "artifact"):
        lines.append(f"[{section}]")
        for note in section_intro.get(section, []):
            lines.append(note)
        for path, default, comment in by_section.get(section, []):
            dotted = ".".join(path[1:]) if len(path) > 1 else path[0]
            tail = _format_default(default)
            lines.append(f"#{dotted} = {tail}   # {comment}")
        lines.append("")
    # env var reference
    lines.append("# --- env vars (override all config files; CLI --extra wins over env) ---")
    for var, path, comment in _ENV_OVERRIDES:
        flat = ".".join(path) if path else "(root)"
        lines.append(f"# {var} -> {flat}   # {comment}")
    lines.append("")
    return lines


def write_default_template(path: Path) -> bool:
    """Write the first-run template to ``path`` if it does not already exist.

    Returns True if a new file was written, False if it was already present.
    Parent directories are created as needed.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_template_lines()), encoding="utf-8")
    return True
