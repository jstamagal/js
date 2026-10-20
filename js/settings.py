"""Setting registry and config loader for the js harness.

`REGISTRY` (a list of `SettingSpec`) names every runtime setting: its storage
path, type, env-var override, empty-state display, and help text. The values
a setting starts with live in ONE file, `js/jsrc`, shipped in the package: one
`set` line per setting. `seed_defaults` and `default_value` read it; a missing
or broken `js/jsrc` stops startup with one line naming the path.

A config file is a *script*: each non-comment line is a command (see
`js.setcmd`). The conventional filenames follow the `rc` lineage (`.ircrc`,
`bitchtearc`): global `jsrc`, project `.js/jsrc`, local `.js/jsrc.local`.
There is no TOML — `js --migrate-config` converts a legacy `config.toml` once.

Precedence, lowest to highest:
    js/jsrc < ~/.js/jsrc < project .js/jsrc
        < project .js/jsrc.local < env vars < --extra CLI flag
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import reasoning as _reasoning
from . import messages as msgs

# What a line typed while a turn runs does: now = join the running turn at its
# next tool boundary, batch = one message after the turn, one = one turn per line.
STEER_MODES: tuple[str, ...] = ("now", "batch", "one")

# The order Up offers prompts in: last = newest first, cwd = this directory's first.
HISTORY_ORDERS: tuple[str, ...] = ("last", "cwd")

# The built-in default layer: one `set` line per registered setting.
PACKAGE_JSRC = Path(__file__).with_name("jsrc")


class DefaultsError(SystemExit):
    """`js/jsrc` is missing or holds a line that does not apply. A SystemExit,
    so an uncaught one ends the process with its one-line message."""


CONFIG_PRECEDENCE_LAYERS = (
    "js/jsrc",
    "~/.js/jsrc",
    "project .js/jsrc",
    "project .js/jsrc.local",
    "env vars",
    "--extra CLI flag",
)
CANONICAL_CONFIG_PRECEDENCE = " < ".join(CONFIG_PRECEDENCE_LAYERS)


# Empty-state display semantics. A setting with no value shows one of these:
EMPTY_OFF = "off"        # boolean setting, explicitly false
EMPTY_NONE = "none"      # no value set (rendered "<none>")
EMPTY_UNSET = "unset"    # param deliberately not sent; provider default wins ("<unset>")


@dataclass(frozen=True)
class SettingSpec:
    """One setting. ``key`` is its name, UPPER_SNAKE (`MODEL`); a display
    setting still has a dotted name (`ui.tools`). The key is the storage path
    in the settings dict, split on dots, and the name `set` takes. Its env var
    is ``JS_<KEY>`` (`canonical_env_name`)."""

    key: str
    type: str            # "str" | "int" | "float" | "bool" | "json" | "map"
    env: str | None = None      # an older env var also read; JS_<KEY> wins over it
    empty: str = EMPTY_NONE     # how an unset value renders
    live: bool = True           # settable live in the REPL
    secret: bool = False        # mask the value in `show`
    aliases: tuple[str, ...] = ()  # jsrc verbs that set ``key``: `model X` is `set MODEL X`
    group: str = ""             # what `/set <group>` lists it under; "" = the dotted section

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.key.split("."))

    @property
    def default(self) -> Any:
        """This setting's value in `js/jsrc`; None when that file leaves it unset."""
        return default_value(self.key)

    @property
    def section(self) -> str:
        return self.group or self.path[0]

    @property
    def doc(self) -> str:
        """This setting's help: its entry in `js/help/set.txt`, "" when it has none."""
        return setting_help().get(self.key, "")


# `/help set NAME` text, one entry per setting.
SETTING_HELP_FILE = Path(__file__).parent / "help" / "set.txt"
_help_cache: dict[str, str] | None = None


def setting_help() -> dict[str, str]:
    """NAME -> help text, read once from `SETTING_HELP_FILE`: a NAME alone on
    a line, then its help indented. `#` lines are comments."""
    global _help_cache
    if _help_cache is None:
        entries: dict[str, list[str]] = {}
        current: list[str] | None = None
        for line in SETTING_HELP_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                continue
            if line and not line[0].isspace():
                current = entries.setdefault(line.strip(), [])
            elif current is not None:
                current.append(line.strip())
        _help_cache = {name: " ".join(" ".join(body).split()) for name, body in entries.items()}
    return _help_cache


# Every setting. Order here is the order `/set` lists them in.
REGISTRY: tuple[SettingSpec, ...] = (
    # --- agent ---
    SettingSpec("DEFAULT_AGENT", "str", group="models"),
    # --- model ---
    SettingSpec("MODEL", "str", aliases=("model",), group="models"),
    SettingSpec("OUTPUT_TOKENS_MAX", "int",
                env="JS_MAX_OUTPUT_TOKENS", empty=EMPTY_NONE, group="models"),
    SettingSpec("CONTEXT_SIZE", "int",
                env="JS_CONTEXT_WINDOW", empty=EMPTY_NONE, group="models"),
    SettingSpec("EFFORT", "str",
                env="JS_REASONING", empty=EMPTY_NONE, group="models"),
    SettingSpec("THINKING_BUDGET", "int",
                empty=EMPTY_NONE, group="models"),
    SettingSpec("VISION", "bool", empty=EMPTY_NONE, group="models"),
    # --- banner ---
    SettingSpec("BANNER", "str", group="startup"),
    SettingSpec("MOTD_FILE", "str", empty=EMPTY_NONE, group="startup"),
    SettingSpec("MOTD_DIR", "str", empty=EMPTY_NONE, group="startup"),
    # --- ui ---
    SettingSpec("ui.reasoning", "int"),
    SettingSpec("ui.net", "int"),
    SettingSpec("ui.status_bg", "str"),
    SettingSpec("ui.status_fg", "str"),
    SettingSpec("ui.tools", "int"),
    SettingSpec("ui.tools_quiet", "json"),
    SettingSpec("ui.shell_quiet", "json"),
    SettingSpec("ui.tools_preview_lines", "int"),
    SettingSpec("ui.markdown", "bool"),
    SettingSpec("theme.accent", "str"),
    SettingSpec("theme.ok", "str"),
    SettingSpec("theme.error", "str"),
    SettingSpec("theme.warn", "str"),
    SettingSpec("theme.dim", "str"),
    SettingSpec("theme.panel", "str"),
    SettingSpec("theme.panel_text", "str"),
    SettingSpec("theme.added", "str"),
    SettingSpec("theme.removed", "str"),
    SettingSpec("theme.code", "str"),
    SettingSpec("ui.resume_exchanges", "int"),
    SettingSpec("EDITING_MODE", "str", group="input"),
    SettingSpec("PASTE_IMAGE_KEY", "str", group="input"),
    SettingSpec("PASTE_COLLAPSE_LINES", "int", group="input"),
    SettingSpec("PASTE_COLLAPSE_CHARS", "int", group="input"),
    SettingSpec("PASTE_IMAGE_COMMAND", "str",
                empty=EMPTY_NONE, group="input"),
    # --- history ---
    SettingSpec("HISTORY_FILE", "str",
                empty=EMPTY_NONE, group="history"),
    SettingSpec("HISTORY_ORDER", "str", group="history"),
    SettingSpec("HISTORY_SIZE", "int", group="history"),
    # --- keys ---
    SettingSpec("KEYBINDS_FILE", "str",
                empty=EMPTY_NONE, group="history"),
    # --- tags ---
    SettingSpec("tags.file", "str",
                empty=EMPTY_NONE),
    SettingSpec("tags.threshold", "float"),
    SettingSpec("tags.max", "int"),
    SettingSpec("tags.messages", "int"),
    SettingSpec("tags.message_chars", "int"),
    SettingSpec("tags.model", "str"),
    # --- provider ---
    SettingSpec("PROVIDER", "str", empty=EMPTY_NONE, aliases=("provider",), group="providers"),
    SettingSpec("BASE_URL", "str", empty=EMPTY_NONE, aliases=("baseurl",), group="providers"),
    SettingSpec("API_KEY", "str", empty=EMPTY_NONE, secret=True, aliases=("apikey",), group="providers"),
    SettingSpec("EXTRA_PARAMS", "map",
                empty=EMPTY_NONE, group="providers"),
    # --- limits ---
    SettingSpec("TURN_ROUNDS_MAX", "int", group="limits"),
    SettingSpec("REPLY_TOOL_CALLS_MAX", "int", group="limits"),
    SettingSpec("SHELL_OUTPUT_MAX", "int", group="limits"),
    SettingSpec("TOOL_RESULT_INLINE_MAX", "int", group="limits"),
    SettingSpec("TOOL_RESULT_MAX", "int", group="limits"),
    SettingSpec("FETCH_TIMEOUT", "int", group="limits"),
    SettingSpec("SHELL_ENV_KEEP", "json", group="limits"),
    SettingSpec("BROWSE_TIMEOUT", "int", group="limits"),
    SettingSpec("DOWNLOAD_TIMEOUT", "int", group="limits"),
    SettingSpec("DOWNLOAD_MAX", "int", group="limits"),
    SettingSpec("INLINE_CODE_TIMEOUT", "int", group="limits"),
    SettingSpec("READ_LINES_MAX", "int", group="limits"),
    SettingSpec("FILE_BYTES_MAX", "int", group="limits"),
    SettingSpec("ATTACHMENT_MAX", "int", group="limits"),
    SettingSpec("READ_BYTES_MAX", "int", group="limits"),
    SettingSpec("PARALLEL_RESULTS_MAX", "int", group="limits"),
    SettingSpec("SUBAGENT_DEPTH_MAX", "int", group="limits"),
    SettingSpec("SUBAGENT_WORKERS_MAX", "int", group="limits"),
    # --- swarm ---
    SettingSpec("SWARM_ACTIVE_MAX", "int", group="shell"),
    # --- kernel ---
    SettingSpec("PYTHON_KERNEL", "bool", group="shell"),
    SettingSpec("kernel.verbosity", "str",
                env="JS_KERNEL_VERBOSITY"),
    SettingSpec("kernel.render_max_lines", "int"),
    SettingSpec("SHELL_PROGRAM", "str", group="shell"),
    SettingSpec("SHELL_WAIT", "int", group="shell"),
    SettingSpec("PYTHON_KERNEL_WAIT", "int", group="shell"),
    # --- jail ---
    SettingSpec("JAIL_BIND", "json", group="shell"),
    # --- runtime ---
    SettingSpec("DEBUG_LOG", "bool", empty=EMPTY_OFF, group="runtime"),
    SettingSpec("runtime.trace", "bool",
                env="JS_TRACE", empty=EMPTY_OFF),
    SettingSpec("PARALLEL_TOOLS_MAX", "int", group="runtime"),
    SettingSpec("RETRY_COUNT_MAX", "int", group="runtime"),
    SettingSpec("RETRY_WAIT_MAX", "float", group="runtime"),
    SettingSpec("STREAM_IDLE_TIMEOUT", "float", group="runtime"),
    SettingSpec("PROGRESS_INTERVAL", "float", group="runtime"),
    SettingSpec("OUTPUT_TOKENS_RETRY_MAX", "int", group="runtime"),
    SettingSpec("OUTPUT_RESUMES_MAX", "int", group="runtime"),
    SettingSpec("STEER_MODE", "str", group="runtime"),
    SettingSpec("REQUEST_LOG", "bool", empty=EMPTY_OFF, group="runtime"),
    SettingSpec("REQUEST_LOG_DIR", "str", empty=EMPTY_NONE, group="runtime"),
    SettingSpec("TRANSCRIPT_LOG", "bool", empty=EMPTY_OFF, group="runtime"),
    SettingSpec("TRANSCRIPT_LOG_DIR", "str", empty=EMPTY_NONE, group="runtime"),
    SettingSpec("INLINE_CODE", "bool", empty=EMPTY_OFF, group="runtime"),
    # --- events ---
    SettingSpec("EXEC_TIMEOUT", "int", group="events"),
    SettingSpec("EXEC_OUTPUT_MAX", "int", group="events"),
    # --- compact ---
    SettingSpec("COMPACTION_AUTO", "bool", empty=EMPTY_OFF, group="compaction"),
    SettingSpec("COMPACTION_LOG_DIR", "str", empty=EMPTY_NONE, group="compaction"),
    SettingSpec("CONTEXT_SIZE_PER_MODEL", "map", empty=EMPTY_NONE, group="context"),
    SettingSpec("CONTEXT_SIZE_UNKNOWN", "int",
                empty=EMPTY_NONE, group="context"),
    SettingSpec("COMPACTION_WARN_AT", "float", group="compaction"),
    SettingSpec("COMPACTION_START_AT", "float", group="compaction"),
    SettingSpec("COMPACTION_FORCE_AT", "float", group="compaction"),
    SettingSpec("COMPACTION_HEADROOM_TOKENS", "int", group="compaction"),
    SettingSpec("COMPACTION_REPLY_RESERVE_TOKENS", "int", group="compaction"),
    SettingSpec("COMPACTION_KEEP_RECENT_TOKENS", "int", group="compaction"),
    SettingSpec("COMPACTION_MIN_SAVINGS_TOKENS", "int", group="compaction"),
    SettingSpec("COMPACTION_KEEP_TOOL_RESULTS", "int", group="compaction"),
    SettingSpec("COMPACTION_REATTACH_FILES_MAX", "int", group="compaction"),
    SettingSpec("COMPACTION_REATTACH_TOKENS_MAX", "int", group="compaction"),
    SettingSpec("COMPACTION_REATTACH_FILE_TOKENS_MAX", "int", group="compaction"),
    SettingSpec("COMPACTION_CHARS_PER_TOKEN", "float", group="compaction"),
    SettingSpec("COMPACTION_EXTERNAL_MODEL", "str", group="compaction"),
    SettingSpec("COMPACTION_SUMMARY_TOKENS_MAX", "int", group="compaction"),
    SettingSpec("COMPACTION_SUMMARY_RESULT_CHARS_MAX", "int", group="compaction"),
    SettingSpec("COMPACTION_FAILURES_MAX", "int", group="compaction"),
    SettingSpec("COMPACTION_CACHE_TTL", "int", group="compaction"),
    SettingSpec("COMPACTION_HINT_COMMAND", "str",
                empty=EMPTY_NONE, group="compaction"),
    # --- subagents ---
    SettingSpec("SUBAGENT_INHERIT_MODEL", "bool",
                empty=EMPTY_OFF, group="subagents"),
    SettingSpec("SUBAGENT_MODEL_LOCK", "bool",
                empty=EMPTY_OFF, group="subagents"),
    # --- tools ---
    SettingSpec("TOOL_ALIASES", "json",
                empty=EMPTY_NONE, group="tools"),
    SettingSpec("HTTP_USER_AGENT", "str", group="tools"),
    SettingSpec("JEV_MODEL", "str", group="tools"),
    SettingSpec("TERMINAL_COLUMNS", "int", group="tools"),
    SettingSpec("TERMINAL_ROWS", "int", group="tools"),
    # --- lsp ---
    SettingSpec("LSP_SERVERS", "json", group="lsp"),
    SettingSpec("LSP_TIMEOUT", "int", group="lsp"),
    # --- notebook ---
    SettingSpec("NOTEBOOK_OUTPUT_LINES_MAX", "int", group="notebook"),
    # --- mcp ---
    SettingSpec("MCP_SERVERS", "json",
                empty=EMPTY_NONE, secret=True, group="mcp"),
    SettingSpec("MCP_AGENT_POLICY", "json",
                empty=EMPTY_NONE, group="mcp"),
    SettingSpec("MCP_TIMEOUT", "float", group="mcp"),
    # --- sampling ---
    SettingSpec("TEMPERATURE", "float",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("TOP_P", "float",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("TOP_K", "int",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("REPETITION_PENALTY", "float",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("PRESENCE_PENALTY", "float",
                empty=EMPTY_UNSET, group="sampling"),
)

SPEC_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in REGISTRY}
SPEC_BY_UPPER: dict[str, SettingSpec] = {spec.key.upper(): spec for spec in REGISTRY}
SPEC_BY_ALIAS: dict[str, SettingSpec] = {alias: spec for spec in REGISTRY for alias in spec.aliases}




def spec_for(name: str) -> SettingSpec | None:
    """The spec a `set` name refers to: its key in any case (`model` is
    `MODEL`, `UI.TOOLS` is `ui.tools`), or a jsrc verb alias."""
    return SPEC_BY_KEY.get(name) or SPEC_BY_UPPER.get(name.upper()) or SPEC_BY_ALIAS.get(name)


# ---------------------------------------------------------------------------
# Value coercion (shared by the env layer and the `set` command)
# ---------------------------------------------------------------------------

_TRUE_TOKENS = {"1", "true", "yes", "on"}
_FALSE_TOKENS = {"0", "false", "no", "off"}
_TOOL_ALIAS_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_HEX_COLOUR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def is_hex_colour(value: object) -> bool:
    """True for a `#rrggbb` string, the form the `ui.status_*` colours take."""
    return isinstance(value, str) and _HEX_COLOUR_RE.fullmatch(value) is not None


def is_key_name(value: object) -> bool:
    """True for a key prompt_toolkit can bind: key names separated by spaces,
    e.g. `c-v` or `escape v`. The form `PASTE_IMAGE_KEY` takes."""
    from prompt_toolkit.key_binding import KeyBindings

    keys = value.split() if isinstance(value, str) else []
    if not keys:
        return False
    try:
        KeyBindings().add(*keys)
    except ValueError:
        return False
    return True

# The only values `EFFORT` accepts: the effort ladder in
# js/reasoning.py, with its bottom stop "none" spelled "off" (stored as the
# literal "none"). Everything else is rejected outright — no
# default/auto/unset synonyms. Clearing the setting back to provider-default is
# `set -EFFORT`, never a magic value here.
REASONING_EFFORT_VALUES: tuple[str, ...] = ("off", *_reasoning.EFFORT_LADDER[1:])
_REASONING_EFFORT_ERROR = "expected " + "|".join(REASONING_EFFORT_VALUES)


def steer_mode(value: Any) -> str:
    """The STEER_MODE mode ``value`` names, or its js/jsrc value for anything else."""
    text = str(value or "").strip().lower()
    return text if text in STEER_MODES else default_value("STEER_MODE")


def parse_bool(raw: str) -> bool | None:
    v = raw.strip().lower()
    if v in _TRUE_TOKENS:
        return True
    if v in _FALSE_TOKENS:
        return False
    return None


_NONNEGATIVE_KNOBS = frozenset({
    "RETRY_COUNT_MAX", "RETRY_WAIT_MAX", "STREAM_IDLE_TIMEOUT", "PROGRESS_INTERVAL",
    "OUTPUT_TOKENS_RETRY_MAX", "OUTPUT_RESUMES_MAX",
})


def coerce_value(spec: SettingSpec, raw: str) -> tuple[Any, str | None]:
    """Coerce ``raw`` for ``spec``. Returns (value, error). Values store
    VERBATIM — there is no magic clear-token (no "default"/"auto"/"none"/"unset"
    special-casing); the only way to clear a setting back to its default/unset
    state is `set -key` (see `apply_unset` in `js.setcmd`)."""
    text = raw.strip()
    if spec.key == "EFFORT":
        v = text.lower()
        if v not in REASONING_EFFORT_VALUES:
            return None, _REASONING_EFFORT_ERROR
        return ("none" if v == "off" else v), None
    if spec.key == "STEER_MODE":
        v = text.lower()
        if v not in STEER_MODES:
            return None, msgs.EXPECTED_ONE_OF.text(choices="|".join(STEER_MODES))
        return v, None
    if spec.key == "PROVIDER" and text:
        from . import providers as _providers

        if _providers.get_provider(text) is None:
            return None, msgs.UNKNOWN_PROVIDER_ID.text(provider=text)
        return text, None
    if (spec.key in {"ui.status_bg", "ui.status_fg"} or spec.key.startswith("theme.")) and not is_hex_colour(text):
        return None, msgs.EXPECTED_COLOUR.text(value=text)
    if spec.key == "HISTORY_ORDER":
        if text not in HISTORY_ORDERS:
            return None, msgs.EXPECTED_ONE_OF.text(choices="|".join(HISTORY_ORDERS))
        return text, None
    if spec.key == "EDITING_MODE":
        if text not in ("emacs", "vi"):
            return None, msgs.EXPECTED_ONE_OF.text(choices="emacs|vi")
        return text, None
    if spec.key == "PASTE_IMAGE_KEY":
        if not is_key_name(text):
            return None, msgs.EXPECTED_KEY.text(value=text)
        return " ".join(text.split()), None
    if spec.key == "PASTE_IMAGE_COMMAND":
        try:
            words = shlex.split(text)
        except ValueError as exc:
            return None, f"expected a shell-quoted command: {exc}"
        if not words:
            return None, "expected a command"
        return text, None
    if spec.key == "BASE_URL" and text:
        if not text.startswith(("http://", "https://")):
            return None, msgs.EXPECTED_URL.text(value=text)
        return text, None
    kind = spec.type
    if kind == "bool":
        parsed = parse_bool(text)
        if parsed is None:
            return None, msgs.EXPECTED_ONE_OF.text(choices="on|off")
        return parsed, None
    if kind == "int":
        try:
            value = int(text)
        except ValueError:
            return None, msgs.EXPECTED_INTEGER.text()
        if spec.key in _NONNEGATIVE_KNOBS and value < 0:
            return None, msgs.EXPECTED_NONNEGATIVE_INTEGER.text()
        if spec.key in {"ui.reasoning", "ui.net", "ui.tools"} and value not in range(4):
            return None, msgs.EXPECTED_LEVEL.text()
        if spec.key in {
            "REPLY_TOOL_CALLS_MAX", "SUBAGENT_WORKERS_MAX", "ui.tools_preview_lines",
            "TERMINAL_COLUMNS", "TERMINAL_ROWS", "PARALLEL_TOOLS_MAX",
            "HISTORY_SIZE", "LSP_TIMEOUT", "tags.max", "tags.messages", "tags.message_chars",
        } and value < 1:
            return None, msgs.EXPECTED_POSITIVE_INTEGER.text()
        return value, None
    if kind == "float":
        try:
            number = float(text)
        except ValueError:
            return None, msgs.EXPECTED_NUMBER.text()
        if spec.key in _NONNEGATIVE_KNOBS and number < 0:
            return None, msgs.EXPECTED_NONNEGATIVE_NUMBER.text()
        if spec.key == "MCP_TIMEOUT" and not (math.isfinite(number) and number > 0):
            return None, msgs.EXPECTED_POSITIVE_NUMBER.text()
        if spec.key == "tags.threshold" and not 0 <= number <= 1:
            return None, msgs.EXPECTED_PROBABILITY.text()
        return number, None
    if kind in ("json", "map"):
        if spec.key in {"MCP_SERVERS", "MCP_AGENT_POLICY"}:
            from . import mcp_config

            try:
                value = (
                    mcp_config.parse_servers_json(raw)
                    if spec.key == "MCP_SERVERS"
                    else mcp_config.parse_agents_json(raw)
                )
            except mcp_config.MCPConfigError as exc:
                return None, str(exc)
        else:
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return None, msgs.EXPECTED_JSON.text()
        if kind == "map" and not isinstance(value, dict):
            return None, msgs.EXPECTED_JSON_OBJECT.text()
        if spec.key == "TOOL_ALIASES":
            error = _validate_alias_profiles(value)
            if error is not None:
                return None, error
        if spec.key in {"SHELL_ENV_KEEP", "ui.tools_quiet", "ui.shell_quiet"}:
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                return None, msgs.EXPECTED_ENV_NAMES.text()
        if spec.key == "JAIL_BIND":
            from . import jail

            try:
                jail.parse_binds(value)
            except ValueError as exc:
                return None, str(exc)
        if spec.key in {"MCP_SERVERS", "MCP_AGENT_POLICY"}:
            from . import mcp_config

            try:
                if spec.key == "MCP_SERVERS":
                    mcp_config.parse_servers(value)
                else:
                    mcp_config.parse_agents(value)
            except mcp_config.MCPConfigError as exc:
                return None, str(exc)
        return value, None
    return text, None  # str


def _validate_alias_profiles(value: Any) -> str | None:
    if not isinstance(value, list):
        return msgs.EXPECTED_JSON_LIST.text()
    for profile in value:
        if not isinstance(profile, dict):
            return msgs.EXPECTED_ALIAS_PROFILES.text()
        match = profile.get("match")
        aliases = profile.get("aliases")
        if not isinstance(match, (str, list)) or not isinstance(aliases, dict):
            return msgs.EXPECTED_ALIAS_PROFILES.text()
        if not aliases:
            return msgs.EXPECTED_NONEMPTY_ALIASES.text()
        matches = [match] if isinstance(match, str) else match
        if not matches or any(not isinstance(item, str) or not item.strip() for item in matches):
            return msgs.EXPECTED_NONEMPTY_MATCH.text()
        seen_aliases: set[str] = set()
        for canonical, alias in aliases.items():
            if not isinstance(canonical, str) or _TOOL_ALIAS_NAME_RE.fullmatch(canonical) is None:
                return msgs.EXPECTED_CANONICAL_TOOL_NAMES.text()
            if not isinstance(alias, str) or _TOOL_ALIAS_NAME_RE.fullmatch(alias) is None:
                return msgs.EXPECTED_ALIAS_NAMES.text()
            key = alias.lower()
            if key in seen_aliases:
                return msgs.EXPECTED_UNIQUE_ALIASES.text()
            seen_aliases.add(key)
    return None


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


def parent_spec(key: str) -> SettingSpec | None:
    """The registered setting ``key`` sits under (`EXTRA_PARAMS` for
    `extra_params.organization`, the setting's name in any case), or None."""
    upper = key.upper()
    for spec in REGISTRY:
        if upper.startswith(spec.key.upper() + "."):
            return spec
    return None


def child_key(key: str) -> str:
    """``key`` with the setting it sits under spelled as registered and the
    child's own part as typed: `extra_params.Team` is `EXTRA_PARAMS.Team`."""
    spec = parent_spec(key)
    return key if spec is None else spec.key + key[len(spec.key):]


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
        raise ValueError(msgs.EXTRA_NOT_KEY_VALUE.text(arg=arg))
    raw_key, raw_value = arg.split("=", 1)
    key = raw_key.strip()
    if not key:
        raise ValueError(msgs.EXTRA_EMPTY_KEY.text(arg=arg))
    if raw_value == "":
        raise ValueError(msgs.EXTRA_EMPTY_VALUE.text(arg=arg))
    spec = spec_for(key)
    if spec is not None:
        value, error = coerce_value(spec, raw_value)
        if error is not None:
            raise ValueError(msgs.EXTRA_BAD_VALUE.text(key=spec.key, error=error))
        return spec.path, value
    key = child_key(key)
    path = _parse_dotted_key(key)
    prefix_spec = parent_spec(key)
    if prefix_spec is None or prefix_spec.type != "map":
        raise ValueError(msgs.EXTRA_UNKNOWN_SETTING.text(key=key))
    return path, coerce_extra_value(raw_value)


def apply_cli_extras(settings: dict, extras: list[str]) -> dict:
    for arg in extras:
        path, value = parse_extra_arg(arg)
        set_dotted(settings, path, value)
    return settings


# ---------------------------------------------------------------------------
# Env layer
# ---------------------------------------------------------------------------

def canonical_env_name(key: str) -> str:
    """The env var for a setting: ``NAME`` -> ``JS_NAME`` (a dotted display
    setting ``a.b`` -> ``JS_A_B``)."""
    return "JS_" + key.upper().replace(".", "_")


def env_names_for(spec: SettingSpec) -> tuple[str, ...]:
    """Env vars that feed ``spec``, highest precedence first: ``JS_<KEY>``,
    then the older name it was known by, if any."""
    canon = canonical_env_name(spec.key)
    if spec.env and spec.env != canon:
        return (canon, spec.env)
    return (canon,)


def apply_env_overrides(settings: dict, env: dict[str, str] | None = None) -> dict:
    """Overlay JS_* env vars onto ``settings``: each setting's ``JS_<KEY>``,
    or its older env name when ``JS_<KEY>`` is not set."""
    source = env if env is not None else os.environ
    for spec in REGISTRY:
        for name in env_names_for(spec):
            if name not in source:
                continue
            value, error = coerce_value(spec, source[name])
            if error is not None:
                # garbage in the env: skip rather than clobber a working value,
                # but say so — a silently dropped JS_BASE_URL costs an evening
                msgs.warn(msgs.ENV_SETTING_IGNORED, name=name, error=error)
                continue
            set_dotted(settings, spec.path, value)
            break
    return settings


# ---------------------------------------------------------------------------
# The default layer: js/jsrc
# ---------------------------------------------------------------------------

# (path, settings) of the js/jsrc read last; a different PACKAGE_JSRC rereads.
_package_cache: tuple[Path, dict] | None = None


def _package_settings() -> dict:
    """The settings `js/jsrc` sets, read once per path. Raises `DefaultsError`
    naming the file when it is missing, a line in it does not apply, or a
    registered setting has no line in it."""
    global _package_cache
    path = PACKAGE_JSRC
    if _package_cache is not None and _package_cache[0] == path:
        return _package_cache[1]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise DefaultsError(msgs.line_for(msgs.DEFAULTS_MISSING, file=sys.stderr, path=path)) from None
    from . import setcmd  # lazy: setcmd imports this module

    settings: dict = {}
    listed: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        result = setcmd.apply_config_line(settings, raw)
        if result.error or not result.handled:
            problem = result.error or msgs.DEFAULTS_NOT_A_SET_LINE.text()
            raise DefaultsError(msgs.line_for(msgs.DEFAULTS_BAD_LINE, file=sys.stderr, location=f"{path}:{lineno}",
                                              error=problem))
        parsed = setcmd.split_command(raw)
        if parsed is not None:
            name = parsed[1].split(maxsplit=1)[0] if parsed[0] == "set" else parsed[0]
            spec = spec_for(name.removeprefix("-"))
            if spec is not None:
                listed.add(spec.key)
    unlisted = [spec.key for spec in REGISTRY if spec.key not in listed]
    if unlisted:
        raise DefaultsError(msgs.line_for(msgs.DEFAULTS_UNLISTED, file=sys.stderr, path=path, keys=", ".join(unlisted)))
    _package_cache = (path, settings)
    return settings


def seed_defaults() -> dict:
    """A fresh copy of the settings `js/jsrc` sets: the bottom config layer."""
    return copy.deepcopy(_package_settings())


def default_value(key: str) -> Any:
    """Registered setting ``key``'s value in `js/jsrc`; None when that file leaves
    it unset."""
    spec = spec_for(key)
    path = spec.path if spec is not None else tuple(key.split("."))
    return copy.deepcopy(get_dotted(_package_settings(), path))


def knob(settings: dict | None, key: str) -> Any:
    """Setting ``key`` from a settings store, or its `js/jsrc` value when the
    store does not hold it."""
    spec = spec_for(key)
    path = spec.path if spec is not None else tuple(key.split("."))
    missing = object()
    value = get_dotted(settings or {}, path, missing)
    return default_value(key) if value is missing else value


def knob_attr(obj: Any, attr: str, key: str) -> Any:
    """``obj.attr`` (a Config or ToolContext field), or setting ``key``'s js/jsrc
    value when ``obj`` has no such attribute."""
    value = getattr(obj, attr, None)
    return default_value(key) if value is None else value


# ---------------------------------------------------------------------------
# Collect: js/jsrc < jsrc files < env < CLI extras
# ---------------------------------------------------------------------------


def load_jsrc_files(paths: list[Path], settings: dict) -> list[str]:
    """Apply each existing jsrc script onto ``settings`` in order. Returns a list
    of human-readable warnings (bad/unknown lines) — a single typo never aborts
    the boot."""
    from . import setcmd  # lazy: setcmd imports this module

    warnings: list[str] = []

    def apply_file(path: Path, stack: list[Path]) -> None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return
        stack.append(path)
        for lineno, raw in enumerate(lines, 1):
            parsed = setcmd.split_command(raw)
            if parsed is not None and parsed[0] in setcmd.LOAD_VERBS:
                # A loaded file's settings sit in the jsrc layer too, resolved
                # against the containing file. The REPL replay reports a
                # missing file or a cycle.
                target, error = setcmd.load_path(parsed[1], path.parent)
                if error is None and target not in stack and len(stack) < setcmd.MAX_LOAD_DEPTH:
                    apply_file(target, stack)
                continue
            result = setcmd.apply_config_line(settings, raw, baseline=_package_settings())
            if result.error:
                warnings.append(msgs.SCRIPT_LINE_FAILED.text(path=path, lineno=lineno, error=result.error))
        stack.pop()

    for path in paths:
        if path.exists():
            apply_file(path.resolve(strict=False), [])
    return warnings


def collect_settings(
    config_paths: list[Path] | None = None,
    env: dict[str, str] | None = None,
    extras: list[str] | None = None,
) -> dict:
    """Run precedence: js/jsrc < jsrc files (in order) < env < CLI extras.

    ``config_paths`` defaults to ``~/.js/jsrc``. ``js.config.from_env``
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
# /save — write the changed settings into the user jsrc, in place
# ---------------------------------------------------------------------------

def _config_line_value(spec: SettingSpec, value: Any) -> str:
    """Render ``value`` as the right-hand side of a `set <key> <value>` line —
    the inverse of `coerce_value`, so a saved line reloads to the same value."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _set_line(spec: SettingSpec, value: Any) -> str:
    """`set <key> <value>`, or `set -<key>` when ``value`` is empty."""
    if value is None or value == "":
        return f"set -{spec.key}"
    return f"set {spec.key} {_config_line_value(spec, value)}"


# The commented user jsrc js writes on first run: every setting at its stock
# value under a description, all of it commented out.
JSRC_TEMPLATE = Path(__file__).with_name("jsrc.template")


def _jsrc_key(line: str) -> tuple[str | None, bool]:
    """The setting a jsrc line sets, and whether the line is live. A commented
    `#set <setting> ...` line names its setting too, as not live; any other
    comment is prose and names nothing."""
    from . import setcmd  # lazy: setcmd imports this module

    body = line.strip()
    live = not body.startswith("#")
    if not live and not body.startswith("#set "):
        return None, False
    parsed = setcmd.split_command(body if live else body[1:])
    if parsed is None or not setcmd.config_owns(parsed[0]):
        return None, False
    verb, arg = parsed
    name = arg.split(maxsplit=1)[0] if verb == "set" and arg else verb
    spec = spec_for(name.lstrip("-"))
    return (spec.key if spec else None), live


def _write_jsrc_lines(path: Path, wanted: dict[str, str | None], extra_lines: list[str]) -> int:
    """Edit the jsrc at ``path`` in place. ``wanted`` maps a setting to the
    line it should have, or to None to comment its live line out. A setting
    with a line rewrites its live line, else uncomments its `#set` line, else
    is appended. ``extra_lines`` not in the file yet are appended. Every other
    line is left alone. Returns how many lines changed."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keyed = [_jsrc_key(line) for line in lines]
    live = {key for key, is_live in keyed if key and is_live}
    placed: set[str] = set()
    changed = 0
    for i, (key, is_live) in enumerate(keyed):
        if key not in wanted:
            continue
        line = wanted[key]
        if line is None:
            if is_live:
                lines[i] = "#" + lines[i]
                changed += 1
            continue
        if key in placed or (not is_live and key in live):
            continue
        placed.add(key)
        if lines[i].strip() != line:
            lines[i] = line
            changed += 1
    tail = [line for key, line in wanted.items() if line is not None and key not in placed]
    present = {line.strip() for line in lines}
    tail += [line for line in extra_lines if line.strip() not in present]
    lines += tail
    changed += len(tail)
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def save_settings_to_jsrc(
    path: Path,
    settings: dict,
    *,
    extra_lines: list[str] | None = None,
) -> int:
    """Write ``settings`` into the jsrc at ``path``, editing it in place: a
    setting not at its stock value gets its line (`_write_jsrc_lines`), a
    setting back at its stock value has its live line commented out.
    ``extra_lines`` are `on` and `alias` lines. No copy of the file is made.
    Returns how many lines changed."""
    wanted: dict[str, str | None] = {}
    for spec in REGISTRY:
        value = get_dotted(settings, spec.path)
        if value == spec.default or (value in (None, "") and spec.default in (None, "")):
            wanted[spec.key] = None
        else:
            wanted[spec.key] = _set_line(spec, value)
    return _write_jsrc_lines(path, wanted, extra_lines or [])


def ensure_user_jsrc(path: Path) -> bool:
    """Copy `JSRC_TEMPLATE` to ``path`` when there is no file there. Returns
    whether it wrote one."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(JSRC_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def write_model_id(path: Path, model_id: str) -> None:
    """Make ``model_id`` the `MODEL` the jsrc at ``path`` sets, in place
    (`_write_jsrc_lines`). Raises OSError."""
    _write_jsrc_lines(path, {"MODEL": f"set MODEL {model_id}"}, [])
