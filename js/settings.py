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
import fcntl
import json
import math
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from collections.abc import Iterator, Mapping
from typing import Any

from . import reasoning as _reasoning
from . import messages as msgs
from . import output

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
    """One setting. ``key`` is its name, UPPER_SNAKE (`MODEL`). The key is
    the storage path in the settings dict, split on dots, and the name its
    ``command``, `set` or `fset`, takes. Its env var is ``JS_<KEY>``
    (`canonical_env_name`)."""

    key: str
    type: str            # "str" | "int" | "float" | "bool" | "json" | "map"
    env: str | None = None      # an older env var also read; JS_<KEY> wins over it
    empty: str = EMPTY_NONE     # how an unset value renders
    live: bool = True           # settable live in the REPL
    secret: bool = False        # mask the value in `show`
    aliases: tuple[str, ...] = ()  # jsrc verbs that set ``key``: `model X` is `set MODEL X`
    group: str = ""             # what `/set <group>` lists it under; "" = the dotted section
    command: str = "set"        # the verb that owns it: `set`, or `fset` for how js looks

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
        """This setting's help: its entry in `js/help/set.txt`, or
        `js/help/fset.txt` for one /fset owns; "" when it has none."""
        if self.command == "fset":
            return read_help(FSET_HELP_FILE).get(self.key, "")
        return setting_help().get(self.key, "")


# `/help set NAME` text, one entry per setting; `/help fset NAME` text, one
# entry per /fset entry that is not a format or a palette slot.
SETTING_HELP_FILE = Path(__file__).parent / "help" / "set.txt"
FSET_HELP_FILE = Path(__file__).parent / "help" / "fset.txt"
_help_cache: dict[Path, dict[str, str]] = {}


def read_help(path: Path) -> dict[str, str]:
    """NAME -> help text, read once from the help file at ``path``: a NAME
    alone on a line, then its help indented. `#` lines are comments."""
    if path not in _help_cache:
        entries: dict[str, list[str]] = {}
        current: list[str] | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                continue
            if line and not line[0].isspace():
                current = entries.setdefault(line.strip(), [])
            elif current is not None:
                current.append(line.strip())
        _help_cache[path] = {name: " ".join(" ".join(body).split()) for name, body in entries.items()}
    return _help_cache[path]


def setting_help() -> dict[str, str]:
    """NAME -> help text from `SETTING_HELP_FILE`."""
    return read_help(SETTING_HELP_FILE)


# Every setting. Order here is the order `/set` lists them in.
REGISTRY: tuple[SettingSpec, ...] = (
    SettingSpec("TOOL_ARGUMENT_STRICT", "bool", group="runtime"),
    SettingSpec("JAIL_CLEAN_PRIVATE_DIRS", "bool", group="shell"),
    SettingSpec("JAIL_HIDE_NETWORK_FILESYSTEMS", "bool", group="shell"),
    SettingSpec("JAIL_HIDE_OPERATOR_HOME", "bool", group="shell"),
    SettingSpec("JAIL_BASE_HIDDEN_DIRS", "json", group="shell"),
    SettingSpec("HOOK_SHELL_PROGRAM", "str", group="events"),
    SettingSpec("SWARM_SPAWN_MAX", "int", group="shell"),
    SettingSpec("SESSION_TEXT_AUTO_REFRESH", "bool", group="history"),
    SettingSpec("SESSION_STATUS_MARK", "str", group="history"),
    SettingSpec("SESSION_QUICK_REPLY_CHARACTERS_MAX", "int", group="history"),
    SettingSpec("SESSION_QUICK_TOOL_CALLS_MAX", "int", group="history"),
    SettingSpec("SESSION_QUICK_TURNS_MAX", "int", group="history"),
    SettingSpec("SESSION_SCRIPT_MODES", "json", group="history"),
    SettingSpec("SESSION_HIDDEN_KINDS", "json", group="history"),
    SettingSpec("TOOL_INSTALLER_USER_AGENT", "str", group="tools"),
    SettingSpec("DOWNLOAD_RETRY_WAIT", "int", group="tools"),
    SettingSpec("DOWNLOAD_RETRIES_MAX", "int", group="tools"),
    SettingSpec("DOWNLOAD_CONNECTIONS_MAX", "int", group="tools"),
    SettingSpec("DOWNLOAD_SPLITS_MAX", "int", group="tools"),
    SettingSpec("TOOL_BINARY_PATH_FALLBACK", "bool", group="tools"),
    SettingSpec("TMP_AGE_MAX", "float", group="runtime"),
    SettingSpec("DOTENV_WALK_UP", "bool", group="runtime"),
    SettingSpec("ENDPOINT_DEFAULT_SHAPE", "str", group="providers"),
    SettingSpec("ENDPOINT_DEFAULT_KEY", "str", secret=True, group="providers"),
    SettingSpec("MODEL_CONTEXT_PROBE", "bool", group="runtime"),
    SettingSpec("MODEL_CATALOG_AGE_MAX", "float", group="runtime"),
    SettingSpec("MODEL_CATALOG_AUTO_REFRESH", "bool", group="runtime"),
    SettingSpec("DEEPSEEK_DEFAULT_EFFORT", "str", group="providers"),
    SettingSpec("CODEX_CLIENT_DETECT", "bool", group="providers"),
    SettingSpec("CODEX_CLIENT_VERSION", "str", group="providers"),
    SettingSpec("CODEX_USER_AGENT", "str", group="providers"),
    SettingSpec("CODEX_ORIGINATOR", "str", group="providers"),
    SettingSpec("CODEX_REASONING_SUMMARY", "str", group="providers"),
    SettingSpec("CODEX_TEXT_VERBOSITY", "str", group="providers"),
    SettingSpec("LOCAL_API_KEY_PLACEHOLDER", "str", group="providers"),
    SettingSpec("JSRC_AUTO_MIGRATE", "bool", group="runtime"),
    SettingSpec("PROJECT_INSTRUCTION_FILES", "json", group="runtime"),
    SettingSpec("GLOBAL_INSTRUCTION_FILES", "json", group="runtime"),
    SettingSpec("PASTE_IMAGE_TIMEOUT", "float", group="input"),
    SettingSpec("PASTE_IMAGE_BACKENDS", "json", group="input"),
    SettingSpec("NOTE_TIMESTAMP_FORMAT", "str", group="input"),
    SettingSpec("SPELLCHECK_TIMEOUT", "float", group="input"),
    SettingSpec("SPELLCHECK_LANGUAGE", "str", group="input"),
    SettingSpec("SPINNER_FRAMES", "str", group="display"),
    SettingSpec("SCROLLBACK_LINES_MAX", "int", group="display"),
    SettingSpec("MARKDOWN_HARD_BREAKS", "bool", group="display"),
    SettingSpec("MARKDOWN_INLINE_PADDING", "str", group="display"),
    SettingSpec("EXCHANGE_ARGUMENT_STYLE", "str", group="display"),
    SettingSpec("EXCHANGE_HEADER_STYLE", "str", group="display"),
    SettingSpec("MARKDOWN_HEADING_FOUR_STYLE", "str", group="display"),
    SettingSpec("MARKDOWN_HEADING_THREE_STYLE", "str", group="display"),
    SettingSpec("MARKDOWN_HEADING_TWO_STYLE", "str", group="display"),
    SettingSpec("SHELL_LIVE_LINES_MAX", "int", group="display"),
    SettingSpec("ARGUMENT_CHARACTERS_MAX", "int", group="display"),
    SettingSpec("DIFF_HUNK_GAP", "str", group="display"),
    SettingSpec("DISPLAY_ELLIPSIS", "str", group="display"),
    SettingSpec("ARGUMENT_NEWLINE_MARK", "str", group="display"),
    SettingSpec("TABLE_RULE", "str", group="display"),
    SettingSpec("EXCHANGE_RAIL", "str", group="display"),
    SettingSpec("EXCHANGE_MARK_FAILED", "str", group="display"),
    SettingSpec("EXCHANGE_MARK_OK", "str", group="display"),
    SettingSpec("EXCHANGE_MARK_RUNNING", "str", group="display"),
    SettingSpec("LOOP_BACKOFF_MAX", "int", group="runtime"),
    SettingSpec("LOOP_FAILURES_MAX", "int", group="runtime"),
    SettingSpec("LOOP_ATTEMPTS_MULTIPLIER", "int", group="runtime"),
    SettingSpec("NOTEBOOK_CLEAR_OUTPUTS", "bool", group="notebook"),
    SettingSpec("NOTEBOOK_REQUIRE_READ", "bool", group="notebook"),
    SettingSpec("LSP_ALLOW_HOME_ROOT", "bool", group="lsp"),
    SettingSpec("LSP_ENVIRONMENT_INHERIT", "bool", group="lsp"),
    SettingSpec("TOOLBOX_REQUIRE_BOUND_NAMES", "bool", group="tools"),
    SettingSpec("TOOLBOX_SEED_EXAMPLES", "bool", group="tools"),
    SettingSpec("TYPESAFE_API_KEY", "str", secret=True, group="tools"),
    SettingSpec("SUBAGENT_RESULT_SHARE", "bool", group="tools"),
    SettingSpec("BROWSER_FALLBACK_FILE", "str", group="tools"),
    SettingSpec("TERMINAL_ENVIRONMENT_INHERIT", "bool", group="tools"),
    SettingSpec("TERMINAL_SHELL_PROGRAM", "str", group="tools"),
    SettingSpec("BROWSE_ALLOW_PRIVATE_NETWORK", "bool", group="tools"),
    SettingSpec("BROWSE_STEALTH", "bool", group="tools"),
    SettingSpec("CONTEXT7_API_KEY", "str", secret=True, group="tools"),
    SettingSpec("EXA_API_KEY", "str", secret=True, group="tools"),
    SettingSpec("TAVILY_API_KEY", "str", secret=True, group="tools"),
    SettingSpec("SERPER_API_KEY", "str", secret=True, group="tools"),
    SettingSpec("SHELL_KILL_UNFINISHED", "bool", group="shell"),
    SettingSpec("SHELL_MANAGED_PATH", "bool", group="shell"),
    SettingSpec("SHELL_PIPEFAIL", "bool", group="shell"),
    SettingSpec("SEARCH_ONE_FILE_SYSTEM", "bool", group="tools"),
    SettingSpec("SEARCH_DEDUPLICATE", "bool", group="tools"),
    SettingSpec("READ_DEDUPLICATE", "bool", group="tools"),
    SettingSpec("EDIT_FUZZY", "bool", group="tools"),
    SettingSpec("EDIT_READ_BYPASS_CHARACTERS", "int", group="tools"),
    SettingSpec("WRITE_REQUIRE_OVERWRITE", "bool", group="tools"),
    SettingSpec("FILE_REQUIRE_READ", "bool", group="tools"),
    SettingSpec("TOOL_ERRORS_MAX", "int", group="runtime"),
    SettingSpec("ANTHROPIC_REQUIRED_EFFORT", "str", group="runtime"),
    SettingSpec("ANTHROPIC_THINKING_DISPLAY", "str", group="runtime"),
    SettingSpec("ANTHROPIC_PROMPT_CACHE", "bool", group="runtime"),
    SettingSpec("CLAUDE_PROXY_INCLUDE_REASONING", "bool", group="runtime"),
    SettingSpec("DEEPSEEK_REASONING_TOKENS_MAX", "int", group="runtime"),
    SettingSpec("COMPACTION_CONSECUTIVE_MAX", "int", group="compaction"),
    SettingSpec("COMPACTION_SUMMARY_REQUEST_TOKENS_MAX", "int", group="compaction"),
    SettingSpec("COMPACTION_HINT_TIMEOUT", "int", group="compaction"),
    SettingSpec("COMPACTION_HINT_SHELL_PROGRAM", "str", group="compaction"),
    SettingSpec("COMPACTION_OVERFLOW_KEEP_TOOL_RESULTS", "int", group="compaction"),
    SettingSpec("COMPACTION_OVERFLOW_ROUNDS_MAX", "int", group="compaction"),
    SettingSpec("RETRY_BACKOFF_JITTER", "float", group="runtime"),
    SettingSpec("RETRY_BACKOFF_MAX", "float", group="runtime"),
    SettingSpec("RETRY_BACKOFF_MULTIPLIER", "float", group="runtime"),
    SettingSpec("RETRY_BACKOFF_START", "float", group="runtime"),
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
    # --- display: /fset entries ---
    SettingSpec("REASONING_DISPLAY", "int", group="display", command="fset"),
    SettingSpec("NET_DISPLAY", "int", group="display", command="fset"),
    SettingSpec("TOOLS_DISPLAY", "int", group="display", command="fset"),
    SettingSpec("TOOLS_QUIET", "json", group="display", command="fset"),
    SettingSpec("SHELL_QUIET", "json", group="display", command="fset"),
    SettingSpec("TOOLS_PREVIEW_LINES", "int", group="display", command="fset"),
    SettingSpec("MARKDOWN", "bool", group="display", command="fset"),
    SettingSpec("RESUME_EXCHANGES", "int", group="display", command="fset"),
    SettingSpec("TRACE", "bool", empty=EMPTY_OFF, group="display", command="fset"),
    SettingSpec("KERNEL_VERBOSITY", "str", group="display", command="fset"),
    SettingSpec("KERNEL_LINES_MAX", "int", group="display", command="fset"),
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
    # --- provider ---
    SettingSpec("PROVIDER", "str", empty=EMPTY_NONE, aliases=("provider",), group="providers"),
    SettingSpec("BASE_URL", "str", empty=EMPTY_NONE, aliases=("baseurl",), group="providers"),
    SettingSpec("API_KEY", "str", empty=EMPTY_NONE, secret=True, aliases=("apikey",), group="providers"),
    SettingSpec("EXTRA_HEADERS", "str", empty=EMPTY_NONE, secret=True, group="providers"),
    SettingSpec("PARANOID_MODE", "str", group="providers"),
    SettingSpec("EXTRA_PARAMS", "map",
                empty=EMPTY_NONE, secret=True, group="providers"),
    # --- limits ---
    SettingSpec("TURN_ROUNDS_MAX", "int", group="limits"),
    SettingSpec("REPLY_TOOL_CALLS_MAX", "int", group="limits"),
    SettingSpec("SHELL_OUTPUT_MAX", "int", group="limits"),
    SettingSpec("TOOL_RESULT_INLINE_MAX", "int", group="limits"),
    SettingSpec("TOOL_RESULT_MAX", "int", group="limits"),
    SettingSpec("TOOL_MEDIA_MAX", "int", group="limits"),
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
    SettingSpec("SHELL_PROGRAM", "str", group="shell"),
    SettingSpec("SHELL_WAIT", "int", group="shell"),
    SettingSpec("PYTHON_KERNEL_WAIT", "int", group="shell"),
    # --- jail ---
    SettingSpec("JAIL_BIND", "json", group="shell"),
    SettingSpec("JAIL_HIDE", "json", group="shell"),
    SettingSpec("SILENT_OVERFLOW_RECOVERY", "bool", group="runtime"),
    SettingSpec("SIGNED_REASONING_RECOVERY", "bool", group="runtime"),
    # --- runtime ---
    SettingSpec("DEBUG_LOG", "bool", empty=EMPTY_OFF, group="runtime"),
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
    SettingSpec("SKILLS_DISABLED", "json", group="tools"),
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
    """True for a `#rrggbb` string."""
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
_REASONING_EFFORT_ERROR = msgs.EXPECTED_REASONING_EFFORT.format(choices="|".join(REASONING_EFFORT_VALUES))


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


# The values a sampling setting can take: (lowest, highest), None = open.
_SAMPLING_RANGES: dict[str, tuple[float | None, float | None]] = {
    "TEMPERATURE": (0.0, None),
    "TOP_P": (0.0, 1.0),
    "REPETITION_PENALTY": (0.0, None),
    "PRESENCE_PENALTY": (-2.0, 2.0),
}


def _unquoted(spec: SettingSpec, text: str) -> str:
    """A value that is not JSON as a jsrc line gives it: a JSON string
    literal, the form `/save` writes one a line cannot hold as it is, is the
    string it spells (`from_line_literal`); any other text as it is."""
    return text if spec.type in ("json", "map") else from_line_literal(text)


def coerce_value(spec: SettingSpec, raw: str) -> tuple[Any, str | None]:
    """Coerce ``raw`` for ``spec``. Returns (value, error). Values store
    VERBATIM — there is no magic clear-token (no "default"/"auto"/"none"/"unset"
    special-casing); the only way to clear a setting back to its default/unset
    state is `set -key` (see `apply_unset` in `js.setcmd`). A value that is
    not JSON is read as a jsrc line gives it first (`_unquoted`), so a quoted
    line means what its string would typed. The error for a secret setting
    never repeats any part of the value."""
    value, error = _coerce_value(spec, raw)
    if error is not None and spec.secret:
        # Both forms: as typed, and as decoded from a quoted JSON string.
        error = _without_value(_without_value(error, _unquoted(spec, raw.strip())), raw)
    return value, error


def _without_value(error: str, raw: str) -> str:
    """``error`` with every piece of ``raw`` in it shown as <set>."""
    pieces = sorted({raw.strip(), *re.split(r"[\s;:=,\"'{}\[\]]+", raw)}, key=len, reverse=True)
    for piece in pieces:
        if len(piece) >= 3:
            error = error.replace(piece, "<set>")
    return error


def _coerce_value(spec: SettingSpec, raw: str) -> tuple[Any, str | None]:
    text = _unquoted(spec, raw.strip())
    if spec.key == "EFFORT":
        v = text.lower()
        if v not in REASONING_EFFORT_VALUES:
            return None, _REASONING_EFFORT_ERROR
        return ("none" if v == "off" else v), None
    if spec.key == "STEER_MODE":
        v = text.lower()
        if v not in STEER_MODES:
            return None, msgs.EXPECTED_ONE_OF.said(choices="|".join(STEER_MODES))
        return v, None
    if spec.key == "PARANOID_MODE":
        v = text.lower()
        if v not in ("off", "on", "extra"):
            return None, msgs.EXPECTED_ONE_OF.said(choices="off|on|extra")
        return v, None
    if spec.key == "PROVIDER" and text:
        from . import providers as _providers

        if _providers.get_provider(text) is None:
            return None, msgs.UNKNOWN_PROVIDER_ID.said(provider=text)
        return text, None
    if spec.key == "HISTORY_ORDER":
        if text not in HISTORY_ORDERS:
            return None, msgs.EXPECTED_ONE_OF.said(choices="|".join(HISTORY_ORDERS))
        return text, None
    if spec.key == "EDITING_MODE":
        if text not in ("emacs", "vi"):
            return None, msgs.EXPECTED_ONE_OF.said(choices="emacs|vi")
        return text, None
    if spec.key == "PASTE_IMAGE_KEY":
        if not is_key_name(text):
            return None, msgs.EXPECTED_KEY.said(value=text)
        return " ".join(text.split()), None
    if spec.key == "PASTE_IMAGE_COMMAND":
        try:
            words = shlex.split(text)
        except ValueError as exc:
            return None, msgs.EXPECTED_SHELL_QUOTED_COMMAND.words(exc=exc)
        if not words:
            return None, msgs.EXPECTED_COMMAND.words()
        return text, None
    if spec.key == "EXTRA_HEADERS" and text:
        try:
            parse_header_lines(text)
        except ValueError as exc:
            return None, str(exc)
    if spec.key == "BASE_URL" and text:
        if not text.startswith(("http://", "https://")):
            return None, msgs.EXPECTED_URL.said(value=text)
        return text, None
    kind = spec.type
    if kind == "bool":
        parsed = parse_bool(text)
        if parsed is None:
            return None, msgs.EXPECTED_ONE_OF.said(choices="on|off")
        return parsed, None
    if kind == "int":
        try:
            value = int(text)
        except ValueError:
            return None, msgs.EXPECTED_INTEGER.said()
        if spec.key in _NONNEGATIVE_KNOBS and value < 0:
            return None, msgs.EXPECTED_NONNEGATIVE_INTEGER.said()
        if spec.key == "TOP_K" and value < 0:
            return None, msgs.EXPECTED_NONNEGATIVE_INTEGER.said()
        if spec.key in {"REASONING_DISPLAY", "NET_DISPLAY", "TOOLS_DISPLAY"} and value not in range(4):
            return None, msgs.EXPECTED_LEVEL.said()
        if spec.key in {
            "REPLY_TOOL_CALLS_MAX", "SUBAGENT_WORKERS_MAX", "TOOLS_PREVIEW_LINES", "KERNEL_LINES_MAX",
            "TERMINAL_COLUMNS", "TERMINAL_ROWS", "PARALLEL_TOOLS_MAX",
            "HISTORY_SIZE", "LSP_TIMEOUT",
        } and value < 1:
            return None, msgs.EXPECTED_POSITIVE_INTEGER.said()
        return value, None
    if kind == "float":
        try:
            number = float(text)
        except ValueError:
            return None, msgs.EXPECTED_NUMBER.said()
        if not math.isfinite(number):  # nan or inf is no setting's value
            return None, msgs.EXPECTED_FINITE_NUMBER.said()
        low, high = _SAMPLING_RANGES.get(spec.key, (None, None))
        if (low is not None and number < low) or (high is not None and number > high):
            return None, (msgs.EXPECTED_AT_LEAST.said(low=low) if high is None
                          else msgs.EXPECTED_RANGE.said(low=low, high=high))
        if spec.key in _NONNEGATIVE_KNOBS and number < 0:
            return None, msgs.EXPECTED_NONNEGATIVE_NUMBER.said()
        if spec.key == "MCP_TIMEOUT" and not (math.isfinite(number) and number > 0):
            return None, msgs.EXPECTED_POSITIVE_NUMBER.said()
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
                return None, msgs.EXPECTED_JSON.said()
        if kind == "map" and not isinstance(value, dict):
            return None, msgs.EXPECTED_JSON_OBJECT.said()
        if spec.key == "TOOL_ALIASES":
            error = _validate_alias_profiles(value)
            if error is not None:
                return None, error
        if spec.key in {"SHELL_ENV_KEEP", "TOOLS_QUIET", "SHELL_QUIET"}:
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                return None, msgs.EXPECTED_ENV_NAMES.said()
        if spec.key == "SKILLS_DISABLED":
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                return None, msgs.EXPECTED_SKILL_NAMES.said()
        if spec.key in {"JAIL_BIND", "JAIL_HIDE"}:
            from . import jail

            try:
                (jail.parse_binds if spec.key == "JAIL_BIND" else jail.parse_hides)(value)
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


# What an HTTP/1.1 request can carry (RFC 9110): a name is a token, a value
# is printable ASCII, spaces and tabs.
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_HEADER_VALUE = re.compile(r"[\t\x20-\x7e]*")


def parse_header_lines(text: str | None) -> dict[str, str]:
    """``Name: value`` entries, curl -H style, separated by ';' or newlines,
    as a dict. Blank entries are skipped. Raises ValueError naming the first
    entry that has no ':', or a name or value a request cannot carry."""
    headers: dict[str, str] = {}
    for entry in (text or "").replace("\n", ";").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, value = entry.partition(":")
        name, value = name.strip(), value.strip()
        if not sep or not _HEADER_NAME.fullmatch(name) or not _HEADER_VALUE.fullmatch(value):
            raise ValueError(msgs.SETTING_NOT_A_HEADER_WANT_NAME_VALUE.format(entry=entry))
        headers[name] = value
    return headers


def _validate_alias_profiles(value: Any) -> str | None:
    if not isinstance(value, list):
        return msgs.EXPECTED_JSON_LIST.said()
    for profile in value:
        if not isinstance(profile, dict):
            return msgs.EXPECTED_ALIAS_PROFILES.said()
        match = profile.get("match")
        aliases = profile.get("aliases")
        if not isinstance(match, (str, list)) or not isinstance(aliases, dict):
            return msgs.EXPECTED_ALIAS_PROFILES.said()
        if not aliases:
            return msgs.EXPECTED_NONEMPTY_ALIASES.said()
        matches = [match] if isinstance(match, str) else match
        if not matches or any(not isinstance(item, str) or not item.strip() for item in matches):
            return msgs.EXPECTED_NONEMPTY_MATCH.said()
        seen_aliases: set[str] = set()
        for canonical, alias in aliases.items():
            if not isinstance(canonical, str) or _TOOL_ALIAS_NAME_RE.fullmatch(canonical) is None:
                return msgs.EXPECTED_CANONICAL_TOOL_NAMES.said()
            if not isinstance(alias, str) or _TOOL_ALIAS_NAME_RE.fullmatch(alias) is None:
                return msgs.EXPECTED_ALIAS_NAMES.said()
            key = alias.lower()
            if key in seen_aliases:
                return msgs.EXPECTED_UNIQUE_ALIASES.said()
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
        raise ValueError(msgs.SETTING_EMPTY_KEY.format(key=key))
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
        raise ValueError(msgs.EXTRA_NOT_KEY_VALUE.said(arg=arg))
    raw_key, raw_value = arg.split("=", 1)
    key = raw_key.strip()
    if not key:
        raise ValueError(msgs.EXTRA_EMPTY_KEY.said(arg=arg))
    if raw_value == "":
        raise ValueError(msgs.EXTRA_EMPTY_VALUE.said(arg=arg))
    spec = spec_for(key)
    if spec is not None:
        value, error = coerce_value(spec, raw_value)
        if error is not None:
            raise ValueError(msgs.EXTRA_BAD_VALUE.said(key=spec.key, error=error))
        return spec.path, value
    key = child_key(key)
    path = _parse_dotted_key(key)
    prefix_spec = parent_spec(key)
    if prefix_spec is None or prefix_spec.type != "map":
        raise ValueError(msgs.EXTRA_UNKNOWN_SETTING.said(key=key))
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
        missing = object()
        value = env_value(spec, source, missing, warn=True)
        if value is not missing:
            set_dotted(settings, spec.path, value)
    return settings


def env_value(spec: SettingSpec, env: Mapping[str, str], missing: Any = None, *, warn: bool = False) -> Any:
    """The value the environment gives ``spec``: its first env name, JS_<KEY>
    then the older one, that holds a value `set` takes. An invalid one is
    passed over (and named, with ``warn``) and the next name is tried.
    ``missing`` when none gives one."""
    for name in env_names_for(spec):
        if name not in env:
            continue
        value, error = coerce_value(spec, env[name])
        if error is not None:
            # garbage in the env: skip rather than clobber a working value,
            # but say so — a silently dropped JS_BASE_URL costs an evening
            if warn:
                output.notice(msgs.ENV_SETTING_IGNORED, name=name, error=error)
            continue
        return value
    return missing


# ---------------------------------------------------------------------------
# The default layer: js/jsrc
# ---------------------------------------------------------------------------

# (path, settings) of the js/jsrc read last; a different PACKAGE_JSRC rereads.
_package_cache: tuple[Path, dict] | None = None


def _defaults_error(message: msgs.Message, **fields) -> BaseException:
    return DefaultsError(msgs.line_for(message, file=sys.stderr, **fields))


def _package_settings(*, error_factory=None) -> dict:
    """The settings `js/jsrc` sets, read once per path. Raises `DefaultsError`
    naming the file when it is missing, a line in it does not apply, or a
    registered setting has no line in it."""
    global _package_cache
    fail = _defaults_error if error_factory is None else error_factory
    path = PACKAGE_JSRC
    if _package_cache is not None and _package_cache[0] == path:
        return _package_cache[1]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise fail(msgs.DEFAULTS_MISSING, path=path) from None
    from . import setcmd  # lazy: setcmd imports this module

    settings: dict = {}
    listed: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        result = setcmd.apply_config_line(settings, raw)
        if result.error or not result.handled:
            problem = result.error or msgs.DEFAULTS_NOT_A_SET_LINE.said()
            raise fail(msgs.DEFAULTS_BAD_LINE, location=f"{path}:{lineno}", error=problem)
        parsed = setcmd.split_command(raw)
        if parsed is not None:
            name = setcmd.first_word(parsed[1])[0] if parsed[0] in ("set", "fset") else parsed[0]
            spec = spec_for(name.removeprefix("-"))
            if spec is not None:
                listed.add(spec.key)
    unlisted = [spec.key for spec in REGISTRY if spec.key not in listed]
    if unlisted:
        raise fail(msgs.DEFAULTS_UNLISTED, path=path, keys=", ".join(unlisted))
    _package_cache = (path, settings)
    return settings


def seed_defaults(*, error_factory=None) -> dict:
    """A fresh copy of the settings `js/jsrc` sets: the bottom config layer."""
    source = _package_settings() if error_factory is None else _package_settings(error_factory=error_factory)
    return copy.deepcopy(source)


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


_live_settings: ContextVar[dict | None] = ContextVar("js_live_settings", default=None)


# The store bound last in any context: a thread started without a copy of
# the binding context reads it.
_process_settings: dict | None = None
# Startup policy read once per cwd, environment and invocation.
_policy_cache: tuple[tuple, dict] | None = None


def bind_live_settings(settings: dict | None) -> None:
    """Bind the current invocation or turn's live store in this execution context."""
    global _process_settings
    _live_settings.set(settings)
    _process_settings = settings


def _startup_policy() -> dict:
    global _policy_cache
    from . import config

    key = (os.getcwd(), frozenset(os.environ.items()), repr(sorted(config._invocation.items())))
    if _policy_cache is None or _policy_cache[0] != key:
        _policy_cache = (key, config.run_settings(migrate=False))
    return _policy_cache[1]


def live_value(key: str, settings: dict | None = None) -> Any:
    """Read explicit settings, the bound turn store, or startup policy without migration."""
    store = settings if settings is not None else _live_settings.get()
    if store is None:
        store = _process_settings
    if store is None:
        store = _startup_policy()
    return knob(store, key)


# ---------------------------------------------------------------------------
# Collect: js/jsrc < jsrc files < env < CLI extras
# ---------------------------------------------------------------------------


# What config load left for the REPL to run, per top-level jsrc: each line
# the settings layer does not own (`alias`, `on`, any other command), in the
# order config load met it inside the files it loaded, already expanded; and
# each `load` that could not run, as its refusal. The replay runs these and
# reads, resolves and expands nothing again.
def _apply_jsrc(path: Path, settings: dict, stack: list[Path], warnings: list[str],
                lines: list[str] | None = None) -> str | None:
    """Apply the settings lines of the jsrc at ``path`` (or ``lines``, read
    as that file) onto ``settings``, the files it loads with it, in order. A
    loaded file stops at its first bad setting, as `/load` stops it, and so
    does every loaded file containing it; the top-level jsrc goes on. Every
    other line is the REPL's, which runs the whole file again in one pass
    (`cli._run_rc_commands`). Returns why a loaded file stopped, or None."""
    from . import setcmd  # lazy: setcmd imports this module

    if lines is None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            return msgs.LOAD_UNREADABLE.said(path=path, error=f"{type(exc).__name__}: {exc}")
    loaded = bool(stack)
    stack.append(path)
    try:
        for lineno, raw in enumerate(lines, 1):
            # $NAMEs expand once, before the line is read as any command.
            raw = setcmd.expand_line(raw, settings)
            parsed = setcmd.split_command(raw)
            error = None
            if parsed is not None and parsed[0] in setcmd.LOAD_VERBS:
                # A loaded file's lines sit in the jsrc layer too, resolved
                # against the containing file, at the point the load stands.
                target, error = setcmd.load_path(parsed[1], path.parent)
                if error is None and len(stack) >= setcmd.MAX_LOAD_DEPTH:
                    error = msgs.LOAD_TOO_DEEP.said()
                elif error is None and target in stack:
                    error = msgs.LOAD_CYCLE.said(path=target)
                elif error is None and not target.is_file():
                    error = msgs.LOAD_NOT_FOUND.said(path=target)
                if error is None:
                    error = _apply_jsrc(target, settings, stack, warnings)
            else:
                result = setcmd.apply_config_line(settings, raw, baseline=_package_settings())
                if result.handled and result.error:
                    error = msgs.SCRIPT_LINE_FAILED.said(path=path, lineno=lineno, error=result.error)
                    warnings.append(error)
            if loaded and error is not None:
                return error
    finally:
        stack.pop()
    return None


def _auto_migrate(paths: list[Path], settings: dict, env: dict[str, str] | None,
                  extras: list[str] | None) -> bool:
    """Resolve migration policy before any source file is rewritten."""
    from . import fset

    probe = copy.deepcopy(settings)
    with fset.held():
        for path in paths:
            if path.is_file():
                _apply_jsrc(path.resolve(strict=False), probe, [], [])
    spec = SPEC_BY_KEY["JSRC_AUTO_MIGRATE"]
    enabled = env_value(spec, os.environ if env is None else env, knob(probe, spec.key))
    for extra in extras or ():
        name, separator, raw = extra.partition("=")
        if separator and name.strip() == spec.key:
            value, error = coerce_value(spec, raw)
            if error is None:
                enabled = value
    return bool(enabled)


def load_jsrc_files(paths: list[Path], settings: dict, *, env: dict[str, str] | None = None,
                    extras: list[str] | None = None, migrate: bool | None = None) -> list[str]:
    """Apply each existing jsrc script's settings onto ``settings`` in order,
    each first rewritten from the old setting names when it still holds them
    (`rename.migrate_jsrc_file`). Returns a list of human-readable warnings
    (bad lines) — a single typo never aborts the boot."""
    from .rename import migrate_jsrc_file  # lazy: rename imports this module

    migrate = _auto_migrate(paths, settings, env, extras) if migrate is None else migrate
    warnings: list[str] = []
    for path in paths:
        if path.exists():
            target = path.resolve(strict=False)
            if migrate:
                migrate_jsrc_file(target)
            _apply_jsrc(target, settings, [], warnings)
    return warnings


# The layers each list of jsrc files was last collected with, to collect it
# again (`collect_again`) or to lay over a jsrc layer the REPL ran
# (`over_jsrc_layer`).
_COLLECTED: dict[tuple[Path, ...], tuple[dict[str, str] | None, list[str] | None]] = {}


def collect_settings(
    config_paths: list[Path] | None = None,
    env: dict[str, str] | None = None,
    extras: list[str] | None = None,
    migrate: bool | None = None,
    *,
    defaults=None,
    reset_formats=None,
) -> dict:
    """Run precedence: js/jsrc < jsrc files (in order) < env < CLI extras.

    ``config_paths`` defaults to ``~/.js/jsrc``. ``js.config.from_env``
    passes the global, project, and project-local files explicitly.
    """
    settings = (seed_defaults if defaults is None else defaults)()
    from . import fset

    # The /fset entries the jsrc files set are rebuilt with the settings.
    (fset.reset if reset_formats is None else reset_formats)()

    from . import paths as _paths
    paths = config_paths if config_paths is not None else [_paths.global_config_file()]
    load_jsrc_files(paths, settings, env=env, extras=extras, migrate=migrate)

    apply_env_overrides(settings, env=env)
    if extras:
        apply_cli_extras(settings, extras)
    _COLLECTED[tuple(paths)] = (env, list(extras) if extras else None)
    return settings


def collect_again(paths: list[Path]) -> dict:
    """The settings ``paths`` give under the env and CLI layers they were last
    collected with (`collect_settings`); the jsrc layer alone when they were
    never collected."""
    if tuple(paths) not in _COLLECTED:
        settings = seed_defaults()
        load_jsrc_files(paths, settings)
        return settings
    env, extras = _COLLECTED[tuple(paths)]
    return collect_settings(paths, env=env, extras=extras)


def over_jsrc_layer(paths: list[Path], layer: dict) -> dict:
    """``layer``, the settings the jsrc files ``paths`` set, under the env and
    CLI layers they were last collected with."""
    settings = copy.deepcopy(layer)
    env, extras = _COLLECTED.get(tuple(paths), ({}, None))
    apply_env_overrides(settings, env=env)
    if extras:
        apply_cli_extras(settings, extras)
    return settings


# ---------------------------------------------------------------------------
# /save — write the changed settings into the user jsrc, in place
# ---------------------------------------------------------------------------

def config_value_text(value: Any) -> str:
    """``value`` as `set` takes it: on/off, JSON for a list or a map, else
    the text — the inverse of `coerce_value`."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


# What a jsrc line cannot hold as it is: a break `str.splitlines` splits a
# file at, or space at either end, which a command strips; nor an empty
# value, which a command reads as none. A value that starts with a quote is
# quoted too, so it never reads as a literal.
_UNHELD = re.compile("[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]|^\\s|\\s$|^\"")


def line_literal(text: str) -> str:
    """``text`` as one jsrc line holds it: as it is, or, when a line cannot
    hold it as it is, a JSON string literal, every break in it escaped."""
    if text and not _UNHELD.search(text):
        return text
    quoted = json.dumps(text, ensure_ascii=False)
    for char in "\x85\u2028\u2029":
        quoted = quoted.replace(char, f"\\u{ord(char):04x}")
    return quoted


def from_line_literal(text: str) -> str:
    """A string value as a jsrc line or a typed command gives it: a JSON
    string literal is the string it spells (`line_literal`), anything else
    is itself."""
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        try:
            value = json.loads(text)
        except ValueError:
            return text
        if isinstance(value, str):
            return value
    return text


def jsrc_text(name: str, text: str) -> str:
    """``text`` as the value on a jsrc line for setting or /fset entry
    ``name``, so the line loads back to ``text`` (`line_literal`): a `$` is
    written `$$`, since a jsrc line's `$NAME`s expand when it loads, except
    in a format or colour, which keeps its `$NAME`s for when it is drawn."""
    from . import fset  # lazy: fset reads settings

    text = line_literal(text)
    return text if fset.read_when_drawn(name) else text.replace("$", "$$")


def _config_line_value(spec: SettingSpec, value: Any) -> str:
    """Render ``value`` as the right-hand side of a `set <key> <value>` line,
    so a saved line reloads to the same value (`jsrc_text`)."""
    if spec.key == "EFFORT" and value == "none":
        value = "off"
    return jsrc_text(spec.key, config_value_text(value))


def _set_line(spec: SettingSpec, value: Any) -> str:
    """`set <key> <value>`, or `set -<key>` when ``value`` is None; `fset`
    for an entry /fset owns. An empty value is written `""`."""
    if value is None:
        return f"{spec.command} -{spec.key}"
    return f"{spec.command} {spec.key} {_config_line_value(spec, value)}"


# How `_jsrc_key` names an `fset` line for an entry `js.fset` holds.
FSET_KEY = "fset:"

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
    if not live and not body.startswith(("#set ", "#fset ")):
        return None, False
    parsed = setcmd.split_command(body if live else body[1:])
    if parsed is None or not setcmd.config_owns(parsed[0]):
        return None, False
    verb, arg = parsed
    name = setcmd.first_word(arg)[0] if verb in ("set", "fset") and arg else verb
    spec = spec_for(name.lstrip("-"))
    if spec is None and verb == "fset" and name:
        return FSET_KEY + name.lstrip("-"), live
    return (spec.key if spec else None), live


def _edited_jsrc_lines(lines: list[str], wanted: dict[str, str | None], extra_lines: list[str],
                       unsets: dict[str, str]) -> tuple[list[str], int]:
    """``lines`` of a jsrc edited so each setting in ``wanted`` gets the line
    it maps to, or its stock value for None, with ``extra_lines`` not in it
    yet appended; and how many lines changed.

    A setting's last live line is rewritten, else its `#set` line
    uncommented. When a `load` comes after that line, the loaded file could
    set the setting back, so the line goes at the end instead; the earlier
    lines stay, since a `load` between them may read them. A setting back at
    its stock value has its live lines with no `load` after them commented
    out, and its line in ``unsets`` (`set -NAME`) appended when one with a
    `load` after it is left. Every other line is left alone."""
    from . import setcmd  # lazy: setcmd imports this module

    lines = list(lines)
    keyed = [_jsrc_key(line) for line in lines]
    loads = [i for i, line in enumerate(lines)
             if (parsed := setcmd.split_command(line)) is not None and parsed[0] in setcmd.LOAD_VERBS]

    def before_a_load(index: int) -> bool:
        return bool(loads) and loads[-1] > index

    live: dict[str, list[int]] = {}
    commented: dict[str, list[int]] = {}
    for index, (key, is_live) in enumerate(keyed):
        if key is not None:
            (live if is_live else commented).setdefault(key, []).append(index)
    changed = 0
    tail: list[str] = []
    for key, line in wanted.items():
        last = live[key][-1] if key in live else None
        if line is None:
            for index in live.get(key, []):
                if not before_a_load(index):
                    lines[index] = "#" + lines[index]
                    changed += 1
            if last is not None and before_a_load(last) and key in unsets:
                tail.append(unsets[key])
            continue
        if last is not None and not before_a_load(last):
            if lines[last].strip() != line:
                lines[last] = line
                changed += 1
        elif last is None and key in commented and not before_a_load(commented[key][0]):
            lines[commented[key][0]] = line
            changed += 1
        else:
            tail.append(line)
    present = {line.strip() for line in lines}
    tail += [line for line in extra_lines if line.strip() not in present]
    return lines + tail, changed + len(tail)


@contextmanager
def _locked_jsrc(path: Path) -> Iterator[None]:
    """Hold the config directory across a jsrc read, edit and write."""
    fd = os.open(path.resolve(strict=False).parent, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write_jsrc_lines(path: Path, wanted: dict[str, str | None], extra_lines: list[str],
                      unsets: dict[str, str] | None = None) -> int:
    """Edit the jsrc at ``path`` in place (`_edited_jsrc_lines`). Returns how
    many lines changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked_jsrc(path):
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        lines, changed = _edited_jsrc_lines(lines, wanted, extra_lines, unsets or {})
        if changed:
            _write_lines(path, lines)
        return changed


def _serialized(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _write_lines(path: Path, lines: list[str]) -> None:
    path = path.resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    staging = Path(name)
    try:
        staging.write_text(_serialized(lines), encoding="utf-8")
        if path.exists():
            staging.chmod(path.stat().st_mode & 0o7777)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def _reloaded(path: Path, text: str) -> tuple[dict, dict[str, str]]:
    """The settings and /fset entries a fresh start would load from ``text``
    as the jsrc at ``path``, read the way startup reads a file, the files it
    loads with it. The live ones, and the banner the messages speak behind,
    are left as they are."""
    from . import fset  # lazy: fset reads settings

    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    loaded = seed_defaults()
    banner = msgs.BANNER
    try:
        with fset.held():
            _apply_jsrc(path.resolve(strict=False), loaded, [], [], lines=lines)
            entries = fset.overrides()
    finally:
        msgs.set_banner(banner)
    return loaded, entries


def _at_stock(spec: SettingSpec, value: Any) -> bool:
    return value == spec.default or (value in (None, "") and spec.default in (None, ""))


def save_settings_to_jsrc(
    path: Path,
    settings: dict,
    *,
    extra_lines: list[str] | None = None,
) -> int:
    """Write ``settings`` and the /fset entries into the jsrc at ``path``,
    editing it in place (`_edited_jsrc_lines`). The edited file is then
    loaded in memory the way a fresh start loads it, files it loads
    included; each setting or entry that comes back different from the live
    one gets its line, or its clearing line, appended, which nothing after
    it can set back. ``extra_lines`` are `on` and `alias` lines. No copy of
    the file is made. Returns how many lines changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked_jsrc(path):
        return _save_settings_to_jsrc(path, settings, extra_lines=extra_lines)


def _save_settings_to_jsrc(
    path: Path, settings: dict, *, extra_lines: list[str] | None = None,
) -> int:
    from . import fset  # lazy: fset reads settings

    wanted: dict[str, str | None] = {}
    unsets: dict[str, str] = {}
    for spec in REGISTRY:
        value = knob(settings, spec.key)
        unsets[spec.key] = _set_line(spec, None)
        wanted[spec.key] = None if _at_stock(spec, value) else _set_line(spec, value)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for key, _live in map(_jsrc_key, existing):
        if key is not None and key.startswith(FSET_KEY):
            wanted[key] = None
            unsets[key] = f"fset -{key[len(FSET_KEY):]}"
    entries = fset.overrides()
    for name, value in entries.items():
        wanted[FSET_KEY + name] = f"fset {name} {jsrc_text(name, value)}"
    lines, changed = _edited_jsrc_lines(existing, wanted, extra_lines or [], unsets)

    loaded, loaded_entries = _reloaded(path, _serialized(lines))
    tail: list[str] = []
    for spec in REGISTRY:
        value, back = knob(settings, spec.key), knob(loaded, spec.key)
        if not (back == value or (_at_stock(spec, value) and _at_stock(spec, back))):
            tail.append(wanted[spec.key] or unsets[spec.key])
    for name in sorted(set(entries) | set(loaded_entries)):
        if loaded_entries.get(name) != entries.get(name):
            tail.append(wanted.get(FSET_KEY + name) or f"fset -{name}")
    if changed or tail:
        _write_lines(path, lines + tail)
    return changed + len(tail)


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
    _write_jsrc_lines(path, {"MODEL": _set_line(SPEC_BY_KEY["MODEL"], model_id)}, [])
