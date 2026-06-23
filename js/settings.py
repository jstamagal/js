"""Knob registry and config loader for the js harness.

ONE registry (`REGISTRY`, a list of `SettingSpec`) is the single source of truth
for every runtime knob: its storage path, type, default, env-var override,
empty-state display, and doc text. From it we generate the env layer, the
first-run config template, the `set`/`show` command surface (see `js.setcmd`),
and the docs.

The config file is a *script*: each non-comment line is a `set <key> <value>`
command (see `js.setcmd`). The conventional filenames follow the `rc` lineage
(`.ircrc`, `bitchtearc`): global `jsrc`, project `.js/jsrc`, local
`.js/jsrc.local`. There is no TOML — `js --migrate-config` converts a legacy
`config.toml` once.

Precedence, lowest to highest:
    built-in defaults < platform jsrc < project .js/jsrc
        < project .js/jsrc.local < env vars < --extra CLI flag
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Built-in defaults — the value used when no config file or env var supplies one.
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
DEFAULT_COMPACT_MODEL = "same"
DEFAULT_COMPACT_SUMMARY_MAX_TOKENS = 4096
DEFAULT_ARTIFACT_DIR = "/srv/artifacts"
DEFAULT_ARTIFACT_URL = "http://localhost"
DEFAULT_ARTIFACT_BIN = "artifact"


CONFIG_PRECEDENCE_LAYERS = (
    "built-in defaults",
    "platform jsrc",
    "project .js/jsrc",
    "project .js/jsrc.local",
    "env vars",
    "--extra CLI flag",
)
CANONICAL_CONFIG_PRECEDENCE = " < ".join(CONFIG_PRECEDENCE_LAYERS)
TEMPLATE_CONFIG_PRECEDENCE = CANONICAL_CONFIG_PRECEDENCE.replace(
    "platform jsrc", "this file", 1
)


# Empty-state display semantics. A knob with no value shows one of these:
EMPTY_OFF = "off"        # boolean knob, explicitly false
EMPTY_NONE = "none"      # no value set (rendered "<none>")
EMPTY_UNSET = "unset"    # param deliberately not sent; provider default wins ("<unset>")


@dataclass(frozen=True)
class SettingSpec:
    """One runtime knob. ``key`` is the canonical dotted name — it is both the
    storage path in the settings dict and the name used by `set`/`show`."""

    key: str
    type: str            # "str" | "int" | "float" | "bool" | "json" | "map"
    default: Any
    doc: str
    env: str | None = None      # JS_* env var feeding the env layer, if any
    empty: str = EMPTY_NONE     # how an unset value renders
    live: bool = True           # settable live in the REPL
    secret: bool = False        # mask the value in `show`

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.key.split("."))

    @property
    def section(self) -> str:
        return self.path[0]


# The single source of truth. Order here is the order `show`/the template use.
REGISTRY: tuple[SettingSpec, ...] = (
    # --- model ---
    SettingSpec("model.id", "str", DEFAULT_MODEL,
                "Default model id; unprefixed ids route through AI Gateway.",
                env="JS_MODEL"),
    SettingSpec("model.max_output_tokens", "int", None,
                "Per-call max_tokens; unset = models.dev metadata when known, else no explicit cap.",
                env="JS_MAX_OUTPUT_TOKENS", empty=EMPTY_NONE),
    SettingSpec("model.reasoning_effort", "str", None,
                "Thinking effort: off|low|medium|high|max|xhigh.",
                env="JS_REASONING", empty=EMPTY_NONE),
    # --- provider ---
    SettingSpec("provider.id", "str", None,
                "Explicit js provider id (e.g. deepseek, openai-codex, ollama).",
                env="JS_PROVIDER", empty=EMPTY_NONE),
    SettingSpec("provider.base_url", "str", None,
                "Explicit provider base URL; unset = provider default.",
                env="JS_BASE_URL", empty=EMPTY_NONE),
    SettingSpec("provider.api_key", "str", None,
                "Explicit provider API key; unset = env/login default.",
                env="JS_API_KEY", empty=EMPTY_NONE, secret=True),
    SettingSpec("provider.extra", "map", {},
                "Free-form extra params passed through to the provider SDK.",
                empty=EMPTY_NONE),
    # --- limits ---
    SettingSpec("limits.max_tool_iterations", "int", DEFAULT_MAX_TOOL_ITERATIONS,
                "Max tool calls per turn before the loop gives up.",
                env="JS_MAX_TOOL_ITERATIONS"),
    SettingSpec("limits.max_bash_output_bytes", "int", DEFAULT_MAX_BASH_OUTPUT_BYTES,
                "Hard cap on shell stdout per call.",
                env="JS_MAX_BASH_OUTPUT_BYTES"),
    SettingSpec("limits.max_tool_result_bytes", "int", DEFAULT_MAX_TOOL_RESULT_BYTES,
                "Hard cap on any tool result string.",
                env="JS_MAX_TOOL_RESULT_BYTES"),
    SettingSpec("limits.fetch_timeout_s", "int", DEFAULT_FETCH_TIMEOUT_S,
                "fetch() per-request timeout in seconds.",
                env="JS_FETCH_TIMEOUT"),
    SettingSpec("limits.max_read_lines", "int", DEFAULT_MAX_READ_LINES,
                "Maximum lines returned by read()."),
    SettingSpec("limits.max_line_chars", "int", DEFAULT_MAX_LINE_CHARS,
                "Maximum characters shown per read/search line."),
    SettingSpec("limits.jsonl_max_line_chars", "int", DEFAULT_JSONL_MAX_LINE_CHARS,
                "Maximum characters shown per read line for .jsonl files only.",
                env="JS_JSONL_MAX_LINE_CHARS"),
    SettingSpec("limits.max_file_bytes", "int", DEFAULT_MAX_FILE_BYTES,
                "Maximum file bytes read by fs tools."),
    SettingSpec("limits.task_max_depth", "int", DEFAULT_TASK_MAX_DEPTH,
                "Maximum recursive task/subagent depth."),
    SettingSpec("limits.wiki_vault_lock_timeout_s", "int", DEFAULT_WIKI_VAULT_LOCK_TIMEOUT_S,
                "Wiki vault lock timeout in seconds."),
    # --- runtime ---
    SettingSpec("runtime.debug", "bool", False,
                "Append per-event records to state/<agent>/debug.log.",
                env="JS_DEBUG", empty=EMPTY_OFF),
    SettingSpec("runtime.trace", "bool", DEFAULT_TRACE,
                "Pretty-print the tool-call trace line as the model runs.",
                env="JS_TRACE", empty=EMPTY_OFF),
    # --- compact ---
    SettingSpec("compact.auto", "bool", DEFAULT_COMPACT_AUTO,
                "Automatic cache-aware context compaction.", empty=EMPTY_OFF),
    SettingSpec("compact.context_window", "int", DEFAULT_COMPACT_CONTEXT_WINDOW,
                "Context window tokens for fullness math; unset = models.dev metadata.",
                empty=EMPTY_NONE),
    SettingSpec("compact.notify_threshold", "float", DEFAULT_COMPACT_NOTIFY_THRESHOLD,
                "Notify once when context reaches this fraction."),
    SettingSpec("compact.trigger_threshold", "float", DEFAULT_COMPACT_TRIGGER_THRESHOLD,
                "Auto-compact at this fullness fraction."),
    SettingSpec("compact.force_threshold", "float", DEFAULT_COMPACT_FORCE_THRESHOLD,
                "Force compact at this fullness fraction."),
    SettingSpec("compact.tail_tokens", "int", DEFAULT_COMPACT_TAIL_TOKENS,
                "Recent tail budget retained after compaction."),
    SettingSpec("compact.min_savings_tokens", "int", DEFAULT_COMPACT_MIN_SAVINGS_TOKENS,
                "Skip compaction unless estimated savings exceeds this."),
    SettingSpec("compact.chars_per_token", "float", DEFAULT_COMPACT_CHARS_PER_TOKEN,
                "Fallback/self-calibrating character-to-token estimate."),
    SettingSpec("compact.model", "str", DEFAULT_COMPACT_MODEL,
                "Model used to write the compaction summary; 'same' = active model."),
    SettingSpec("compact.summary_max_tokens", "int", DEFAULT_COMPACT_SUMMARY_MAX_TOKENS,
                "Max tokens for the compaction summary (hard-capped at 8192)."),
    SettingSpec("compact.pre_hook", "str", None,
                "Optional shell command whose stdout guides compaction.",
                empty=EMPTY_NONE),
    # --- subagents ---
    SettingSpec("subagents.prefer_inherit", "bool", False,
                "Subagents inherit the parent's model when true; else use the agent's own primary.",
                empty=EMPTY_OFF),
    SettingSpec("subagents.lock_model", "bool", False,
                "When true, the main agent cannot pick a subagent model via the task tool.",
                empty=EMPTY_OFF),
    # --- tools ---
    SettingSpec("tools.alias_profiles", "json", None,
                "Model-facing tool-name alias profiles: list of {match:[...], aliases:{...}}.",
                empty=EMPTY_NONE),
    # --- sampling ---
    SettingSpec("sampling.temperature", "float", None,
                "Provider-default sampling temperature; unset = do not send.",
                empty=EMPTY_UNSET),
    SettingSpec("sampling.top_p", "float", None,
                "Provider-default nucleus sampling top_p; unset = do not send.",
                empty=EMPTY_UNSET),
    SettingSpec("sampling.top_k", "int", None,
                "Provider-default top_k sampling; unset = do not send.",
                empty=EMPTY_UNSET),
    SettingSpec("sampling.repetition_penalty", "float", None,
                "Provider-default repetition penalty; unset = do not send.",
                empty=EMPTY_UNSET),
    SettingSpec("sampling.presence_penalty", "float", None,
                "Provider-default presence penalty; unset = do not send.",
                empty=EMPTY_UNSET),
    # --- wiki ---
    SettingSpec("wiki.aliases", "map", {},
                "Vault alias map; set sub-keys, e.g. `set wiki.aliases.creative /path`.",
                empty=EMPTY_NONE),
    # --- artifact ---
    SettingSpec("artifact.dir", "str", None, "Artifact library directory (default /srv/artifacts; ARTIFACT_DIR env also honored).", empty=EMPTY_NONE),
    SettingSpec("artifact.url", "str", None, "Artifact HTTP base URL (default http://localhost; ARTIFACT_URL env also honored).", empty=EMPTY_NONE),
    SettingSpec("artifact.bin", "str", None, "Artifact CLI binary (default artifact; ARTIFACT_BIN env also honored).", empty=EMPTY_NONE),
)

SPEC_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in REGISTRY}
KNOWN_SECTIONS: frozenset[str] = frozenset(spec.section for spec in REGISTRY)
SECTION_ORDER: tuple[str, ...] = (
    "model",
    "provider",
    "limits",
    "runtime",
    "compact",
    "subagents",
    "tools",
    "sampling",
    "wiki",
    "artifact",
)


# ---------------------------------------------------------------------------
# Value coercion (shared by the env layer and the `set` command)
# ---------------------------------------------------------------------------

_TRUE_TOKENS = {"1", "true", "yes", "on"}
_FALSE_TOKENS = {"0", "false", "no", "off"}
_NULL_TOKENS = {"off", "none", "unset", "default", "auto", ""}


def parse_bool(raw: str) -> bool | None:
    v = raw.strip().lower()
    if v in _TRUE_TOKENS:
        return True
    if v in _FALSE_TOKENS:
        return False
    return None


def coerce_value(spec: SettingSpec, raw: str) -> tuple[Any, str | None]:
    """Coerce ``raw`` for ``spec``. Returns (value, error). A nullable knob
    accepts off/none/unset/default/auto/"" as "clear to default-provider"."""
    text = raw.strip()
    if spec.key == "model.reasoning_effort" and text.lower() in {"off", "none", "0"}:
        return "none", None
    if spec.empty in (EMPTY_NONE, EMPTY_UNSET) and text.lower() in _NULL_TOKENS:
        return None, None
    kind = spec.type
    if kind == "bool":
        parsed = parse_bool(text)
        if parsed is None:
            return None, "expected on/off"
        return parsed, None
    if kind == "int":
        try:
            return int(text), None
        except ValueError:
            return None, "expected an integer"
    if kind == "float":
        try:
            return float(text), None
        except ValueError:
            return None, "expected a number"
    if kind in ("json", "map"):
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None, "expected a JSON value"
        if kind == "map" and not isinstance(value, dict):
            return None, "expected a JSON object"
        if spec.key == "tools.alias_profiles" and not isinstance(value, list):
            return None, "expected a JSON list"
        return value, None
    return text, None  # str


# ---------------------------------------------------------------------------
# Dotted-path helpers
# ---------------------------------------------------------------------------

def set_dotted(target: dict, path: tuple[str, ...], value: Any) -> None:
    """Place ``value`` at ``path`` in ``target``, creating dicts as needed."""
    cursor = target
    for part in path[:-1]:
        node = cursor.get(part)
        if not isinstance(node, dict):
            node = {}
            cursor[part] = node
        cursor = node
    cursor[path[-1]] = value


def get_dotted(settings: dict, path: tuple[str, ...], default: Any = None) -> Any:
    """Read ``path`` from ``settings`` with a default when any segment is missing."""
    cursor: Any = settings
    for part in path:
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


def _parse_dotted_key(key: str) -> tuple[str, ...]:
    parts = tuple(p for p in key.split(".") if p)
    if not parts:
        raise ValueError(f"empty key: {key!r}")
    return parts


# ---------------------------------------------------------------------------
# CLI --extra one-shots
# ---------------------------------------------------------------------------

def coerce_extra_value(raw: str) -> Any:
    """Coerce a CLI ``--extra KEY=VALUE`` right-hand side: int, then float, then
    bool/null tokens, else string."""
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
    """Parse one ``--extra KEY=VALUE`` argument into (path, value)."""
    if "=" not in arg:
        raise ValueError(f"--extra expects KEY=VALUE, got: {arg!r}")
    raw_key, raw_value = arg.split("=", 1)
    key = raw_key.strip()
    if not key:
        raise ValueError(f"--extra key is empty: {arg!r}")
    if raw_value == "":
        raise ValueError(f"--extra value is empty: {arg!r}")
    if key in {"model.reasoning_effort", "provider.extra", "tools.alias_profiles"}:
        spec = SPEC_BY_KEY[key]
        value, error = coerce_value(spec, raw_value)
        if error is not None:
            raise ValueError(f"--extra {key}: {error}")
        return spec.path, value
    return _parse_dotted_key(key), coerce_extra_value(raw_value)


def apply_cli_extras(settings: dict, extras: list[str]) -> dict:
    for arg in extras:
        path, value = parse_extra_arg(arg)
        set_dotted(settings, path, value)
    return settings


# ---------------------------------------------------------------------------
# Env layer
# ---------------------------------------------------------------------------

def apply_env_overrides(settings: dict, env: dict[str, str] | None = None) -> dict:
    """Overlay JS_* env vars (declared in the registry) onto ``settings``."""
    source = env if env is not None else os.environ
    for spec in REGISTRY:
        if not spec.env or spec.env not in source:
            continue
        value, error = coerce_value(spec, source[spec.env])
        if error is not None:
            # garbage in the env: skip rather than clobber a working value
            continue
        set_dotted(settings, spec.path, value)
    return settings


# ---------------------------------------------------------------------------
# Collect: defaults < jsrc files < env < CLI extras
# ---------------------------------------------------------------------------

def seed_defaults() -> dict:
    settings: dict = {}
    for spec in REGISTRY:
        if spec.default is not None:
            set_dotted(settings, spec.path, copy.deepcopy(spec.default))
    return settings


def load_jsrc_files(paths: list[Path], settings: dict) -> list[str]:
    """Apply each existing jsrc script onto ``settings`` in order. Returns a list
    of human-readable warnings (bad/unknown lines) — a single typo never aborts
    the boot."""
    from . import setcmd  # lazy: setcmd imports this module

    warnings: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            result = setcmd.apply_config_line(settings, raw)
            if result.error:
                warnings.append(f"{path}:{lineno}: {result.error}")
    return warnings


def collect_settings(
    config_paths: list[Path] | None = None,
    env: dict[str, str] | None = None,
    extras: list[str] | None = None,
) -> dict:
    """Run precedence: built-in defaults < jsrc files (in order) < env < CLI extras.

    ``config_paths`` defaults to the platform jsrc file. ``js.config.from_env``
    passes the global, project, and project-local files explicitly.
    """
    settings = seed_defaults()

    from . import paths as _paths
    paths = config_paths if config_paths is not None else [_paths.global_config_file()]
    load_jsrc_files(paths, settings)

    apply_env_overrides(settings, env=env)
    if extras:
        apply_cli_extras(settings, extras)
    return settings


# ---------------------------------------------------------------------------
# First-run template (a commented jsrc set-script)
# ---------------------------------------------------------------------------

_SECTION_INTRO: dict[str, list[str]] = {
    "model": ["# Default model + per-call model knobs."],
    "provider": [
        "# Optional explicit provider id / base_url / api_key.",
        "# Leave unset to let ai-python route model ids natively.",
    ],
    "limits": ["# Per-call / per-turn caps."],
    "runtime": ["# Live-runtime toggles."],
    "compact": ["# Cache-first context compaction knobs."],
    "subagents": ["# Subagent model-selection policy."],
    "tools": ["# Model-facing tool aliasing."],
    "sampling": ["# Per-turn sampling overrides. Default display is <unset>; provider/model defaults win."],
    "wiki": ["# Wiki vault aliases, e.g. `set wiki.aliases.creative /path/to/wiki`."],
    "artifact": ["# Artifact system defaults."],
}


def _template_value(spec: SettingSpec) -> str:
    default = spec.default
    if default is None:
        return EMPTY_UNSET if spec.empty == EMPTY_UNSET else ""
    if isinstance(default, bool):
        return "on" if default else "off"
    if isinstance(default, (dict, list)):
        return json.dumps(default) if default else ""
    return str(default)


def _template_lines() -> list[str]:
    """Build the commented jsrc template written on first run. Each knob is shown
    as a commented-out `set` line with its default; uncomment and edit."""
    lines: list[str] = [
        "# js config — generated on first run.",
        "#",
        "# This file is a script: each non-comment line is a `set <key> <value>`",
        "# command, applied at startup. Uncomment a line and edit the value.",
        "#",
        f"# Precedence, lowest to highest: {TEMPLATE_CONFIG_PRECEDENCE}.",
        "",
        "# --- stock defaults (active lines; edit or delete) ---",
        f"set model.id {DEFAULT_MODEL}",
        "set wiki.aliases.creative ~/wiki-creative",
        "set wiki.aliases.general ~/wiki-general",
        "",
    ]
    by_section: dict[str, list[SettingSpec]] = {}
    for spec in REGISTRY:
        by_section.setdefault(spec.section, []).append(spec)
    for section in SECTION_ORDER:
        specs = by_section.get(section)
        if not specs:
            continue
        lines.append(f"# === {section} ===")
        lines.extend(_SECTION_INTRO.get(section, []))
        for spec in specs:
            lines.append(f"# {spec.doc}")
            set_line = f"#set {spec.key} {_template_value(spec)}".rstrip()
            lines.append(set_line)
        lines.append("")
    lines.append("# --- env vars (override config files; --extra wins over env) ---")
    for spec in REGISTRY:
        if spec.env:
            lines.append(f"# {spec.env} -> set {spec.key}")
    lines.append("")
    return lines


def write_default_template(path: Path) -> bool:
    """Write the first-run jsrc template to ``path`` if absent. Returns True when
    a new file was written, False if it already existed."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_template_lines()), encoding="utf-8")
    return True
