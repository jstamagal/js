"""Setting registry and the default layer for the js harness.

`REGISTRY` (a list of `SettingSpec`) names every runtime setting: its storage
path, type, env-var override, empty-state display, and help text. The values
a setting starts with live in ONE file, `defaults/jsrc`, shipped in the package: one
`set` line per setting. `seed_defaults` and `default_value` read it; a missing
or broken `defaults/jsrc` stops startup with one line naming the path. The
layers over it are `js.setting_layers`.
"""

from __future__ import annotations

import copy
import os
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import jsrc_line, messages as msgs, paths

# The built-in default layer: one `set` line per registered setting.
PACKAGE_JSRC = paths.defaults_dir() / "jsrc"


class DefaultsError(SystemExit):
    """`defaults/jsrc` is missing or holds a line that does not apply. A SystemExit,
    so an uncaught one ends the process with its one-line message."""


# Empty-state display semantics. A setting with no value shows one of these:
EMPTY_OFF = "off"        # boolean setting, explicitly false
EMPTY_NONE = "none"      # no value set (rendered "<none>")
EMPTY_UNSET = "unset"    # param deliberately not sent; provider default wins ("<unset>")


@dataclass(frozen=True)
class SettingSpec:
    """One setting. ``key`` is its name, UPPER_SNAKE (`MODEL`). The key is
    the storage path in the settings dict, split on dots, and the name its
    `set` takes. Its env var is ``JS_<KEY>``
    (`canonical_env_name`)."""

    key: str
    type: str            # "str" | "int" | "float" | "bool" | "json" | "map"
    env: str | None = None      # another env var also read (an older name, or the vendor's own); JS_<KEY> wins over it
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
        """This setting's value in `defaults/jsrc`; None when that file leaves it unset."""
        return default_value(self.key)

    @property
    def section(self) -> str:
        return self.group or self.path[0]

    @property
    def doc(self) -> str:
        """This setting's help: its entry in `defaults/help/set.txt`; "" when it
        has none."""
        return setting_help().get(self.key, "")


# `/help set NAME` text, one entry per setting; `/help fset NAME` text, one
# entry per /fset entry that is not a format or a palette slot.
SETTING_HELP_FILE = paths.defaults_dir() / "help" / "set.txt"
FSET_HELP_FILE = paths.defaults_dir() / "help" / "fset.txt"
_help_cache: dict[Path, dict[str, str]] = {}


def read_help(path: Path) -> dict[str, str]:
    """NAME -> help text, read once from the help file at ``path``: a NAME
    alone on a line, then its help indented. `#` lines are comments."""
    if path in (SETTING_HELP_FILE, FSET_HELP_FILE):
        path = paths.find(Path("help") / path.name) or path
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
    SettingSpec("REMOVE_TRASH", "bool", group="tools"),
    SettingSpec("TRASH_BYTES_MAX", "int", group="tools"),
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
    SettingSpec("ENDPOINT_DEFAULT_KEY", "str", secret=True, group="providers"),
    SettingSpec("MODEL_CONTEXT_PROBE", "bool", group="runtime"),
    SettingSpec("MODEL_CATALOG_AGE_MAX", "float", group="runtime"),
    SettingSpec("MODEL_CATALOG_AUTO_REFRESH", "bool", group="runtime"),
    SettingSpec("LOCAL_API_KEY_PLACEHOLDER", "str", group="providers"),
    SettingSpec("PROJECT_INSTRUCTION_FILES", "json", group="runtime"),
    SettingSpec("GLOBAL_INSTRUCTION_FILES", "json", group="runtime"),
    SettingSpec("PASTE_IMAGE_TIMEOUT", "float", group="input"),
    SettingSpec("PASTE_IMAGE_BACKENDS", "json", group="input"),
    SettingSpec("NOTE_TIMESTAMP_FORMAT", "str", group="input"),
    SettingSpec("SPELLCHECK_TIMEOUT", "float", group="input"),
    SettingSpec("SPELLCHECK_LANGUAGE", "str", group="input"),
    SettingSpec("SPINNER_FRAMES", "str", group="display"),
    SettingSpec("SCROLLBACK_LINES_MAX", "int", group="display"),
    SettingSpec("WINDOW_MAX", "int", group="display"),
    SettingSpec("WINDOW_KEEP_UNSEEN", "bool", group="display"),
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
    SettingSpec("TYPESAFE_API_KEY", "str", env="TYPESAFE_API_KEY", secret=True, group="tools"),
    SettingSpec("SUBAGENT_RESULT_SHARE", "bool", group="tools"),
    SettingSpec("BROWSER_FALLBACK_FILE", "str", group="tools"),
    SettingSpec("TERMINAL_ENVIRONMENT_INHERIT", "bool", group="tools"),
    SettingSpec("TERMINAL_SHELL_PROGRAM", "str", group="tools"),
    SettingSpec("BROWSE_ALLOW_PRIVATE_NETWORK", "bool", group="tools"),
    SettingSpec("BROWSE_STEALTH", "bool", group="tools"),
    SettingSpec("CONTEXT7_API_KEY", "str", env="CONTEXT7_API_KEY", secret=True, group="tools"),
    SettingSpec("EXA_API_KEY", "str", env="EXA_API_KEY", secret=True, group="tools"),
    SettingSpec("TAVILY_API_KEY", "str", env="TAVILY_API_KEY", secret=True, group="tools"),
    SettingSpec("SERPER_API_KEY", "str", env="SERPER_API_KEY", secret=True, group="tools"),
    SettingSpec("SHELL_KILL_UNFINISHED", "bool", group="shell"),
    SettingSpec("KILL_LOG_FILE", "str", empty=EMPTY_NONE, group="shell"),
    SettingSpec("SHELL_MANAGED_PATH", "bool", group="shell"),
    SettingSpec("SHELL_PIPEFAIL", "bool", group="shell"),
    SettingSpec("SEARCH_ONE_FILE_SYSTEM", "bool", group="tools"),
    SettingSpec("SEARCH_DEDUPLICATE", "bool", group="tools"),
    SettingSpec("READ_DEDUPLICATE", "bool", group="tools"),
    SettingSpec("EDIT_FUZZY", "bool", group="tools"),
    SettingSpec("EDIT_READ_BYPASS_CHARACTERS", "int", group="tools"),
    SettingSpec("WRITE_REQUIRE_OVERWRITE", "bool", group="tools"),
    SettingSpec("HELD_WRITE_MIN", "int", group="tools"),
    SettingSpec("HELD_TTL", "int", group="tools"),
    SettingSpec("FILE_REQUIRE_READ", "bool", group="tools"),
    SettingSpec("TOOL_ERRORS_MAX", "int", group="runtime"),
    SettingSpec("ANTHROPIC_REQUIRED_EFFORT", "str", group="runtime"),
    SettingSpec("ANTHROPIC_THINKING_DISPLAY", "str", group="runtime"),
    SettingSpec("ANTHROPIC_PROMPT_CACHE", "bool", group="runtime"),
    SettingSpec("CLAUDE_PROXY_INCLUDE_REASONING", "bool", group="runtime"),
    SettingSpec("DEEPSEEK_REASONING_TOKENS_MAX", "int", group="runtime"),
    SettingSpec("ANTHROPIC_OUTPUT_TOKENS_MAX", "int", group="runtime"),
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
    SettingSpec("IMAGE_MODEL", "str", group="models"),
    SettingSpec("VIDEO_MODEL", "str", group="models"),
    SettingSpec("MUSIC_MODEL", "str", group="models"),
    SettingSpec("SFX_MODEL", "str", group="models"),
    SettingSpec("TTS_MODEL", "str", group="models"),
    SettingSpec("STT_MODEL", "str", group="models"),
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
    SettingSpec("REASONING_DISPLAY", "int", group="display"),
    SettingSpec("NET_DISPLAY", "int", group="display"),
    SettingSpec("TOOLS_DISPLAY", "int", group="display"),
    SettingSpec("TOOLS_QUIET", "json", group="display"),
    SettingSpec("SHELL_QUIET", "json", group="display"),
    SettingSpec("TOOLS_PREVIEW_LINES", "int", group="display"),
    SettingSpec("MARKDOWN", "bool", group="display"),
    SettingSpec("RESUME_EXCHANGES", "int", group="display"),
    SettingSpec("TRACE", "bool", empty=EMPTY_OFF, group="display"),
    SettingSpec("KERNEL_VERBOSITY", "str", group="display"),
    SettingSpec("KERNEL_LINES_MAX", "int", group="display"),
    SettingSpec("EDITING_MODE", "str", group="input"),
    SettingSpec("EXIT_ARM_TIMEOUT", "float", group="input"),
    SettingSpec("PASTE_IMAGE_KEY", "str", group="input"),
    SettingSpec("PASTE_COLLAPSE_LINES", "int", group="input"),
    SettingSpec("PASTE_COLLAPSE_CHARS", "int", group="input"),
    SettingSpec("PASTE_IMAGE_COMMAND", "str",
                empty=EMPTY_NONE, group="input"),
    SettingSpec("COPY_COMMAND", "str",
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
    SettingSpec("READ_BYTES_MAX", "int", group="limits"),
    SettingSpec("PARALLEL_RESULTS_MAX", "int", group="limits"),
    SettingSpec("SUBAGENT_DEPTH_MAX", "int", group="limits"),
    SettingSpec("SUBAGENT_WORKERS_MAX", "int", group="limits"),
    # --- swarm ---
    SettingSpec("SWARM_ACTIVE_MAX", "int", group="shell"),
    # --- acp ---
    SettingSpec("ACP_STEER_WAIT", "int", group="runtime"),
    # --- kernel ---
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
    SettingSpec("CONNECT_TIMEOUT", "float", group="runtime"),
    SettingSpec("FIRST_BYTE_TIMEOUT", "float", group="runtime"),
    SettingSpec("STREAM_IDLE_TIMEOUT", "float", group="runtime"),
    SettingSpec("OUTPUT_TOKENS_RETRY_MAX", "int", group="runtime"),
    SettingSpec("OUTPUT_RESUMES_MAX", "int", group="runtime"),
    SettingSpec("STEER_MODE", "str", group="runtime"),
    SettingSpec("REQUEST_LOG", "bool", empty=EMPTY_OFF, group="runtime"),
    SettingSpec("REQUEST_LOG_DIR", "str", empty=EMPTY_NONE, group="runtime"),
    SettingSpec("TRANSCRIPT_USER_LABEL", "str", group="runtime"),
    SettingSpec("TRANSCRIPT_ASSISTANT_LABEL", "str", group="runtime"),
    SettingSpec("TRANSCRIPT_SPEAKER_FORMAT", "str", group="runtime"),
    SettingSpec("TRANSCRIPT_TIMESTAMP_FORMAT", "str", group="runtime"),
    SettingSpec("TRANSCRIPT_DISABLE_ALL_ON_ERROR", "bool", group="runtime"),
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
    SettingSpec("ATTACHMENT_CONTEXT_MAX", "float", group="compaction"),
    SettingSpec("COMPACTION_EXTERNAL_MODEL", "str", group="compaction"),
    SettingSpec("COMPACTION_SUMMARY_TOKENS_MAX", "int", group="compaction"),
    SettingSpec("COMPACTION_SUMMARY_RESULT_CHARS_MAX", "int", group="compaction"),
    SettingSpec("COMPACTION_FAILURES_MAX", "int", group="compaction"),
    SettingSpec("COMPACTION_CACHE_TTL", "int", group="compaction"),
    SettingSpec("CACHE_KEEPALIVE", "bool", group="compaction"),
    SettingSpec("CACHE_KEEPALIVE_IDLE_MAX", "int", group="compaction"),
    SettingSpec("CACHE_KEEPALIVE_COMPACTION", "bool", group="compaction"),
    SettingSpec("CACHE_KEEPALIVE_COMPACTION_TOKENS_MIN", "int", group="compaction"),
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
    SettingSpec("TOOL_PROFILES", "json",
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
    SettingSpec("MIN_P", "float",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("TOP_N_SIGMA", "float",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("XTC_PROBABILITY", "float",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("XTC_THRESHOLD", "float",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("DRY_MULTIPLIER", "float",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("DRY_BASE", "float",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("DRY_ALLOWED_LENGTH", "int",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("DRY_PENALTY_LAST_N", "int",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("DRY_SEQUENCE_BREAKERS", "json",
                empty=EMPTY_UNSET, group="sampling"),
    SettingSpec("TEMPLATE_KWARGS", "str",
                empty=EMPTY_UNSET, group="sampling"),
)

SPEC_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in REGISTRY}
SPEC_BY_UPPER: dict[str, SettingSpec] = {spec.key.upper(): spec for spec in REGISTRY}
SPEC_BY_ALIAS: dict[str, SettingSpec] = {alias: spec for spec in REGISTRY for alias in spec.aliases}




def spec_for(name: str) -> SettingSpec | None:
    """The spec a `set` name refers to: its key in any case (`model` is
    `MODEL`, `UI.TOOLS` is `ui.tools`), or a jsrc verb alias."""
    return SPEC_BY_KEY.get(name) or SPEC_BY_UPPER.get(name.upper()) or SPEC_BY_ALIAS.get(name)


def canonical_env_name(key: str) -> str:
    """The env var for a setting: ``NAME`` -> ``JS_NAME`` (a dotted display
    setting ``a.b`` -> ``JS_A_B``)."""
    return "JS_" + key.upper().replace(".", "_")


def env_names_for(spec: SettingSpec) -> tuple[str, ...]:
    """Env vars that feed ``spec``, highest precedence first: ``JS_<KEY>``,
    then ``spec.env``, if any."""
    canon = canonical_env_name(spec.key)
    if spec.env and spec.env != canon:
        return (canon, spec.env)
    return (canon,)


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
# The default layer: defaults/jsrc
# ---------------------------------------------------------------------------

# (path, settings) of the defaults/jsrc read last; a different PACKAGE_JSRC rereads.
_package_cache: tuple[Path, dict] | None = None


def _defaults_error(message: msgs.Message, **fields) -> BaseException:
    return DefaultsError(msgs.line_for(message, file=sys.stderr, **fields))


def package_settings() -> dict:
    """The settings `defaults/jsrc` sets, read once per path. Raises `DefaultsError`
    naming the file when it is missing, a line in it does not apply, or a
    registered setting has no line in it."""
    global _package_cache
    path = PACKAGE_JSRC
    if _package_cache is not None and _package_cache[0] == path:
        return _package_cache[1]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise _defaults_error(msgs.DEFAULTS_MISSING, path=path) from None
    from . import setcmd  # lazy: setcmd imports this module

    settings: dict = {}
    listed: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        result = setcmd.apply_config_line(settings, raw)
        if result.error or not result.handled:
            problem = result.error or msgs.DEFAULTS_NOT_A_SET_LINE.said()
            raise _defaults_error(msgs.DEFAULTS_BAD_LINE, location=f"{path}:{lineno}", error=problem)
        parsed = jsrc_line.split_command(raw)
        if parsed is not None:
            name = jsrc_line.first_word(parsed[1])[0] if parsed[0] in ("set", "fset") else parsed[0]
            spec = spec_for(name.removeprefix("-"))
            if spec is not None:
                listed.add(spec.key)
    unlisted = [spec.key for spec in REGISTRY if spec.key not in listed]
    if unlisted:
        raise _defaults_error(msgs.DEFAULTS_UNLISTED, path=path, keys=", ".join(unlisted))
    _package_cache = (path, settings)
    return settings


def seed_defaults() -> dict:
    """A fresh copy of the settings `defaults/jsrc` sets: the bottom config layer."""
    return copy.deepcopy(package_settings())


def default_value(key: str) -> Any:
    """Registered setting ``key``'s value in `defaults/jsrc`; None when that file leaves
    it unset."""
    spec = spec_for(key)
    path = spec.path if spec is not None else tuple(key.split("."))
    return copy.deepcopy(get_dotted(package_settings(), path))


def knob(settings: dict | None, key: str) -> Any:
    """Setting ``key`` from a settings store, or its `defaults/jsrc` value when the
    store does not hold it."""
    spec = spec_for(key)
    path = spec.path if spec is not None else tuple(key.split("."))
    missing = object()
    value = get_dotted(settings or {}, path, missing)
    return default_value(key) if value is missing else value


def knob_attr(obj: Any, attr: str, key: str) -> Any:
    """``obj.attr`` (a Config or ToolContext field), or setting ``key``'s defaults/jsrc
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

    key = (os.getcwd(), frozenset(os.environ.items()), config._invocation)
    if _policy_cache is None or _policy_cache[0] != key:
        _policy_cache = (key, config.run_settings(held=True))
    return _policy_cache[1]


def live_value(key: str, settings: dict | None = None) -> Any:
    """Read explicit settings, the bound turn store, or startup policy without migration."""
    store = settings if settings is not None else _live_settings.get()
    if store is None:
        store = _process_settings
    if store is None:
        store = _startup_policy()
    return knob(store, key)


def live_optional_str_setting(live_settings: dict, path: tuple[str, str], default: str | None) -> str | None:
    missing = object()
    raw = get_dotted(live_settings, path, missing)
    if raw is missing:
        return default
    if raw is None or isinstance(raw, str):
        return raw
    return default


def live_bool_setting(live_settings: dict, path: tuple[str, ...], default: bool) -> bool:
    raw = get_dotted(live_settings, path, default)
    return raw if isinstance(raw, bool) else default
