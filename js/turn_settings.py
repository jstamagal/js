"""The settings a turn reads, projected from the settings store.

`TURN_SETTINGS` is the one table of them. Each `TurnSetting` row names an
attribute and the registered setting (`js.settings.REGISTRY`) it comes from,
with the reader that turns a stored value into the value the turn uses.
Every row is a field of `Config`; an `on_context` row is also a field of
`ToolContext`, copied from the Config at the start of each turn and taken
by a subagent from its parent's context.

- `project(settings, fallback)` reads every row out of a settings store.
- `install(context, cfg)` copies the `on_context` rows from a Config onto a
  ToolContext.
- `inherit(parent)` returns the `on_context` rows of a parent ToolContext.
- `ConfigSettings` and `ContextSettings` are the dataclass bases that
  declare the rows as fields of Config and ToolContext. Their defaults read
  `js/jsrc` when an instance is built.
"""

from __future__ import annotations

from dataclasses import dataclass, field, make_dataclass
from collections.abc import Callable
from typing import Any

from . import settings as _settings

# What a reader returns for a stored value it cannot use.
INVALID: Any = object()


def number(raw: Any) -> Any:
    if raw is None or isinstance(raw, bool):
        return INVALID
    try:
        return float(raw)
    except (TypeError, ValueError, OverflowError):
        return INVALID


def integer(raw: Any) -> Any:
    if raw is None or isinstance(raw, bool):
        return INVALID
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return INVALID


def at_least_one(raw: Any) -> Any:
    value = integer(raw)
    return value if value is INVALID else max(1, value)


def optional_integer(raw: Any) -> Any:
    return None if raw is None else integer(raw)


def flag(raw: Any) -> Any:
    return raw if isinstance(raw, bool) else INVALID


def text(raw: Any) -> Any:
    return raw if isinstance(raw, str) else INVALID


def optional_text(raw: Any) -> Any:
    return raw if raw is None or isinstance(raw, str) else INVALID


def choice(*allowed: str) -> Callable[[Any], Any]:
    def read(raw: Any) -> Any:
        value = str(raw or "").strip().lower()
        return value if value in allowed else INVALID

    return read


def names(raw: Any) -> Any:
    """A list of non-empty strings, as a tuple, each stripped: ` PATH ` would
    match no environment variable."""
    if isinstance(raw, (list, tuple)) and all(isinstance(item, str) and item.strip() for item in raw):
        return tuple(item.strip() for item in raw)
    return INVALID


def paths(raw: Any) -> Any:
    """A list of non-empty path strings, as a tuple; null is no paths."""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)) and all(isinstance(item, str) and item.strip() for item in raw):
        return tuple(raw)
    return INVALID


def header_lines(raw: Any) -> Any:
    """`Name: value` entries (`settings.parse_header_lines`) as a dict; unset is none."""
    if raw is None:
        return {}
    if not isinstance(raw, str):
        return INVALID
    try:
        return _settings.parse_header_lines(raw)
    except ValueError:
        return INVALID


def entries(raw: Any) -> Any:
    return raw if isinstance(raw, list) else INVALID


@dataclass(frozen=True)
class TurnSetting:
    attr: str                    # attribute on Config, and on ToolContext when on_context
    key: str                     # registered setting key
    read: Callable[[Any], Any]   # stored value -> effective value, or INVALID
    on_context: bool = False     # also a ToolContext field; a subagent takes it from its parent

    def default(self) -> Any:
        """The effective value of this setting's `js/jsrc` value."""
        return self.read(_settings.default_value(self.key))


TURN_SETTINGS: tuple[TurnSetting, ...] = (
    TurnSetting("remove_trash", "REMOVE_TRASH", flag, on_context=True),
    TurnSetting("trash_bytes_max", "TRASH_BYTES_MAX", integer, on_context=True),
    TurnSetting("tool_argument_strict", "TOOL_ARGUMENT_STRICT", flag),
    TurnSetting("jail_clean_private_dirs", "JAIL_CLEAN_PRIVATE_DIRS", flag, on_context=True),
    TurnSetting("jail_hide_network_filesystems", "JAIL_HIDE_NETWORK_FILESYSTEMS", flag, on_context=True),
    TurnSetting("jail_hide_operator_home", "JAIL_HIDE_OPERATOR_HOME", flag, on_context=True),
    TurnSetting("jail_base_hidden_dirs", "JAIL_BASE_HIDDEN_DIRS", paths, on_context=True),
    TurnSetting("hook_shell_program", "HOOK_SHELL_PROGRAM", text),
    TurnSetting("swarm_spawn_max", "SWARM_SPAWN_MAX", integer),
    TurnSetting("session_text_auto_refresh", "SESSION_TEXT_AUTO_REFRESH", flag),
    TurnSetting("download_retry_wait", "DOWNLOAD_RETRY_WAIT", integer),
    TurnSetting("download_retries_max", "DOWNLOAD_RETRIES_MAX", integer),
    TurnSetting("download_connections_max", "DOWNLOAD_CONNECTIONS_MAX", integer),
    TurnSetting("download_splits_max", "DOWNLOAD_SPLITS_MAX", integer),
    TurnSetting("tool_binary_path_fallback", "TOOL_BINARY_PATH_FALLBACK", flag),
    TurnSetting("model_context_probe", "MODEL_CONTEXT_PROBE", flag),
    TurnSetting("model_catalog_age_max", "MODEL_CATALOG_AGE_MAX", number),
    TurnSetting("model_catalog_auto_refresh", "MODEL_CATALOG_AUTO_REFRESH", flag),
    TurnSetting("codex_client_detect", "CODEX_CLIENT_DETECT", flag),
    TurnSetting("codex_client_version", "CODEX_CLIENT_VERSION", text),
    TurnSetting("codex_user_agent", "CODEX_USER_AGENT", text),
    TurnSetting("codex_originator", "CODEX_ORIGINATOR", text),
    TurnSetting("codex_reasoning_summary", "CODEX_REASONING_SUMMARY", text),
    TurnSetting("codex_text_verbosity", "CODEX_TEXT_VERBOSITY", text),
    TurnSetting("notebook_clear_outputs", "NOTEBOOK_CLEAR_OUTPUTS", flag, on_context=True),
    TurnSetting("notebook_require_read", "NOTEBOOK_REQUIRE_READ", flag, on_context=True),
    TurnSetting("lsp_allow_home_root", "LSP_ALLOW_HOME_ROOT", flag, on_context=True),
    TurnSetting("lsp_environment_inherit", "LSP_ENVIRONMENT_INHERIT", flag, on_context=True),
    TurnSetting("toolbox_require_bound_names", "TOOLBOX_REQUIRE_BOUND_NAMES", flag, on_context=True),
    TurnSetting("toolbox_seed_examples", "TOOLBOX_SEED_EXAMPLES", flag, on_context=True),
    TurnSetting("typesafe_api_key", "TYPESAFE_API_KEY", text, on_context=True),
    TurnSetting("subagent_result_share", "SUBAGENT_RESULT_SHARE", flag, on_context=True),
    TurnSetting("browser_fallback_file", "BROWSER_FALLBACK_FILE", text, on_context=True),
    TurnSetting("terminal_environment_inherit", "TERMINAL_ENVIRONMENT_INHERIT", flag, on_context=True),
    TurnSetting("terminal_shell_program", "TERMINAL_SHELL_PROGRAM", text, on_context=True),
    TurnSetting("browse_allow_private_network", "BROWSE_ALLOW_PRIVATE_NETWORK", flag, on_context=True),
    TurnSetting("browse_stealth", "BROWSE_STEALTH", flag, on_context=True),
    TurnSetting("context7_api_key", "CONTEXT7_API_KEY", text, on_context=True),
    TurnSetting("exa_api_key", "EXA_API_KEY", text, on_context=True),
    TurnSetting("tavily_api_key", "TAVILY_API_KEY", text, on_context=True),
    TurnSetting("serper_api_key", "SERPER_API_KEY", text, on_context=True),
    TurnSetting("shell_kill_unfinished", "SHELL_KILL_UNFINISHED", flag, on_context=True),
    TurnSetting("shell_managed_path", "SHELL_MANAGED_PATH", flag, on_context=True),
    TurnSetting("shell_pipefail", "SHELL_PIPEFAIL", flag, on_context=True),
    TurnSetting("search_one_file_system", "SEARCH_ONE_FILE_SYSTEM", flag, on_context=True),
    TurnSetting("search_deduplicate", "SEARCH_DEDUPLICATE", flag, on_context=True),
    TurnSetting("read_deduplicate", "READ_DEDUPLICATE", flag, on_context=True),
    TurnSetting("edit_fuzzy", "EDIT_FUZZY", flag, on_context=True),
    TurnSetting("edit_read_bypass_characters", "EDIT_READ_BYPASS_CHARACTERS", integer, on_context=True),
    TurnSetting("write_require_overwrite", "WRITE_REQUIRE_OVERWRITE", flag, on_context=True),
    TurnSetting("file_require_read", "FILE_REQUIRE_READ", flag, on_context=True),
    TurnSetting("tool_errors_max", "TOOL_ERRORS_MAX", integer),
    TurnSetting("anthropic_required_effort", "ANTHROPIC_REQUIRED_EFFORT", text),
    TurnSetting("anthropic_thinking_display", "ANTHROPIC_THINKING_DISPLAY", text),
    TurnSetting("anthropic_prompt_cache", "ANTHROPIC_PROMPT_CACHE", flag),
    TurnSetting("claude_proxy_include_reasoning", "CLAUDE_PROXY_INCLUDE_REASONING", flag),
    TurnSetting("deepseek_reasoning_tokens_max", "DEEPSEEK_REASONING_TOKENS_MAX", integer),
    TurnSetting("compaction_consecutive_max", "COMPACTION_CONSECUTIVE_MAX", integer),
    TurnSetting("compaction_summary_request_tokens_max", "COMPACTION_SUMMARY_REQUEST_TOKENS_MAX", integer),
    TurnSetting("compaction_hint_timeout", "COMPACTION_HINT_TIMEOUT", integer),
    TurnSetting("compaction_hint_shell_program", "COMPACTION_HINT_SHELL_PROGRAM", text),
    TurnSetting("compaction_overflow_keep_tool_results", "COMPACTION_OVERFLOW_KEEP_TOOL_RESULTS", integer),
    TurnSetting("compaction_overflow_rounds_max", "COMPACTION_OVERFLOW_ROUNDS_MAX", integer),
    TurnSetting("retry_backoff_jitter", "RETRY_BACKOFF_JITTER", number),
    TurnSetting("retry_backoff_max", "RETRY_BACKOFF_MAX", number),
    TurnSetting("retry_backoff_multiplier", "RETRY_BACKOFF_MULTIPLIER", number),
    TurnSetting("retry_backoff_start", "RETRY_BACKOFF_START", number),
    # --- on the ToolContext ---
    TurnSetting("max_read_lines", "READ_LINES_MAX", integer, on_context=True),
    TurnSetting("max_file_bytes", "FILE_BYTES_MAX", integer, on_context=True),
    TurnSetting("max_read_bytes", "READ_BYTES_MAX", integer, on_context=True),
    TurnSetting("max_tool_result_bytes", "TOOL_RESULT_MAX", integer, on_context=True),
    TurnSetting("max_bash_output_bytes", "SHELL_OUTPUT_MAX", integer, on_context=True),
    TurnSetting("max_tool_result_inline_bytes", "TOOL_RESULT_INLINE_MAX", integer, on_context=True),
    TurnSetting("max_tool_media_bytes", "TOOL_MEDIA_MAX", integer, on_context=True),
    TurnSetting("fetch_timeout_s", "FETCH_TIMEOUT", integer, on_context=True),
    TurnSetting("browse_timeout_s", "BROWSE_TIMEOUT", integer, on_context=True),
    TurnSetting("download_timeout_s", "DOWNLOAD_TIMEOUT", integer, on_context=True),
    TurnSetting("max_download_bytes", "DOWNLOAD_MAX", integer, on_context=True),
    TurnSetting("task_max_depth", "SUBAGENT_DEPTH_MAX", integer, on_context=True),
    TurnSetting("subagent_max_workers", "SUBAGENT_WORKERS_MAX", integer, on_context=True),
    TurnSetting("shell_env_allow", "SHELL_ENV_KEEP", names, on_context=True),
    TurnSetting("user_agent", "HTTP_USER_AGENT", text, on_context=True),
    TurnSetting("terminal_cols", "TERMINAL_COLUMNS", integer, on_context=True),
    TurnSetting("terminal_rows", "TERMINAL_ROWS", integer, on_context=True),
    TurnSetting("kernel_verbosity", "KERNEL_VERBOSITY", choice("quiet", "normal", "verbose"), on_context=True),
    TurnSetting("kernel_render_max_lines", "KERNEL_LINES_MAX", integer, on_context=True),
    TurnSetting("kernel_wait_seconds", "PYTHON_KERNEL_WAIT", integer, on_context=True),
    TurnSetting("shell_wait_seconds", "SHELL_WAIT", integer, on_context=True),
    TurnSetting("shell_program", "SHELL_PROGRAM", text, on_context=True),
    TurnSetting("max_parallel_tools", "PARALLEL_TOOLS_MAX", at_least_one, on_context=True),
    TurnSetting("jail_bind", "JAIL_BIND", paths, on_context=True),
    TurnSetting("jail_hide", "JAIL_HIDE", paths, on_context=True),
    TurnSetting("lsp_servers", "LSP_SERVERS", entries, on_context=True),
    TurnSetting("lsp_timeout_s", "LSP_TIMEOUT", integer, on_context=True),
    TurnSetting("notebook_output_lines", "NOTEBOOK_OUTPUT_LINES_MAX", integer, on_context=True),
    TurnSetting("jev_model", "JEV_MODEL", text, on_context=True),
    TurnSetting("paranoid_mode", "PARANOID_MODE", choice("off", "on", "extra"), on_context=True),
    TurnSetting("skills_disabled", "SKILLS_DISABLED", names, on_context=True),
    # --- on the Config only ---
    TurnSetting("silent_overflow_recovery", "SILENT_OVERFLOW_RECOVERY", flag),
    TurnSetting("signed_reasoning_recovery", "SIGNED_REASONING_RECOVERY", flag),
    TurnSetting("max_tool_iterations", "TURN_ROUNDS_MAX", integer),
    TurnSetting("max_tool_calls_per_message", "REPLY_TOOL_CALLS_MAX", integer),
    TurnSetting("max_tool_results_per_turn_bytes", "PARALLEL_RESULTS_MAX", integer),
    TurnSetting("inline_code_timeout_s", "INLINE_CODE_TIMEOUT", integer),
    TurnSetting("max_text_attachment_bytes", "ATTACHMENT_MAX", integer),
    TurnSetting("max_output_tokens", "OUTPUT_TOKENS_MAX", optional_integer),
    TurnSetting("model_context_window", "CONTEXT_SIZE", optional_integer),
    TurnSetting("thinking_budget", "THINKING_BUDGET", optional_integer),
    TurnSetting("extra_headers", "EXTRA_HEADERS", header_lines),
    TurnSetting("trace", "TRACE", flag),
    TurnSetting("prefer_inherit", "SUBAGENT_INHERIT_MODEL", flag),
    TurnSetting("lock_subagent_model", "SUBAGENT_MODEL_LOCK", flag),
    TurnSetting("allow_inline_code", "INLINE_CODE", flag),
    TurnSetting("debug_autolog", "REQUEST_LOG", flag),
    TurnSetting("debug_autolog_dir", "REQUEST_LOG_DIR", optional_text),
    TurnSetting("transcript_user_label", "TRANSCRIPT_USER_LABEL", text),
    TurnSetting("transcript_assistant_label", "TRANSCRIPT_ASSISTANT_LABEL", text),
    TurnSetting("transcript_speaker_format", "TRANSCRIPT_SPEAKER_FORMAT", text),
    TurnSetting("transcript_timestamp_format", "TRANSCRIPT_TIMESTAMP_FORMAT", text),
    TurnSetting("transcript_disable_all_on_error", "TRANSCRIPT_DISABLE_ALL_ON_ERROR", flag),
    TurnSetting("transcript_log", "TRANSCRIPT_LOG", flag),
    TurnSetting("transcript_log_dir", "TRANSCRIPT_LOG_DIR", optional_text),
)

_MISSING = object()


def _in_range(row: TurnSetting, value: Any) -> Any:
    """``value``, or INVALID when `set` would refuse it for this knob (a
    negative or zero where the knob needs more): one range rule, the one
    settings.coerce_value keeps, for values that skipped it (a JSON store,
    an agent.yaml)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    spec = _settings.SPEC_BY_KEY.get(row.key)
    if spec is None or spec.type != "int":
        return value
    _value, error = _settings.coerce_value(spec, str(value))
    return INVALID if error else value


def project(settings: dict | None, fallback: object | None = None) -> dict[str, Any]:
    """attr -> effective value for every row, read from ``settings``. A value
    the store does not hold, or holds in a form the row cannot read, is
    ``fallback``'s attribute when a fallback is given, else the js/jsrc value."""
    store = settings if isinstance(settings, dict) else {}
    values: dict[str, Any] = {}
    for row in TURN_SETTINGS:
        raw = _settings.get_dotted(store, tuple(row.key.split(".")), _MISSING)
        value = INVALID if raw is _MISSING else _in_range(row, row.read(raw))
        if value is INVALID:
            value = getattr(fallback, row.attr, _MISSING) if fallback is not None else _MISSING
            if value is _MISSING:
                value = row.default()
        values[row.attr] = value
    return values


def install(context: Any, cfg: Any) -> None:
    """Copy the ``on_context`` rows from ``cfg`` onto ``context``. An attribute
    ``cfg`` does not have leaves the context's value as it is."""
    for row in TURN_SETTINGS:
        if row.on_context and hasattr(cfg, row.attr):
            setattr(context, row.attr, getattr(cfg, row.attr))


def inherit(parent: Any) -> dict[str, Any]:
    """The ``on_context`` rows of ``parent``, as ToolContext keyword arguments."""
    return {
        row.attr: getattr(parent, row.attr)
        for row in TURN_SETTINGS
        if row.on_context and hasattr(parent, row.attr)
    }


def _fields(rows) -> list[tuple[str, Any, Any]]:
    return [(row.attr, Any, field(default_factory=row.default)) for row in rows]


ConfigSettings = make_dataclass(
    "ConfigSettings", _fields(TURN_SETTINGS), frozen=True, kw_only=True, module=__name__,
)
ContextSettings = make_dataclass(
    "ContextSettings", _fields(row for row in TURN_SETTINGS if row.on_context), kw_only=True, module=__name__,
)
