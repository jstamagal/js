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
    """A list of non-empty strings, as a tuple."""
    if isinstance(raw, (list, tuple)) and all(isinstance(item, str) and item.strip() for item in raw):
        return tuple(raw)
    return INVALID


def paths(raw: Any) -> Any:
    """A list of path strings, as a tuple; null is no paths."""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)) and all(isinstance(item, str) for item in raw):
        return tuple(raw)
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
    # --- on the ToolContext ---
    TurnSetting("max_read_lines", "limits.max_read_lines", integer, on_context=True),
    TurnSetting("max_file_bytes", "limits.max_file_bytes", integer, on_context=True),
    TurnSetting("max_read_bytes", "limits.max_read_bytes", integer, on_context=True),
    TurnSetting("max_tool_result_bytes", "limits.max_tool_result_bytes", integer, on_context=True),
    TurnSetting("max_bash_output_bytes", "limits.max_bash_output_bytes", integer, on_context=True),
    TurnSetting("max_bash_output_ceiling", "limits.max_bash_output_ceiling", integer, on_context=True),
    TurnSetting("max_tool_result_inline_bytes", "limits.max_tool_result_inline_bytes", integer, on_context=True),
    TurnSetting("fetch_timeout_s", "limits.fetch_timeout_s", integer, on_context=True),
    TurnSetting("browse_timeout_s", "limits.browse_timeout_s", integer, on_context=True),
    TurnSetting("download_timeout_s", "limits.download_timeout_s", integer, on_context=True),
    TurnSetting("max_download_bytes", "limits.max_download_bytes", integer, on_context=True),
    TurnSetting("task_max_depth", "limits.task_max_depth", integer, on_context=True),
    TurnSetting("subagent_max_workers", "limits.subagent_max_workers", integer, on_context=True),
    TurnSetting("shell_env_allow", "limits.shell_env_allow", names, on_context=True),
    TurnSetting("user_agent", "tools.user_agent", text, on_context=True),
    TurnSetting("terminal_cols", "tools.terminal_cols", integer, on_context=True),
    TurnSetting("terminal_rows", "tools.terminal_rows", integer, on_context=True),
    TurnSetting("kernel_verbosity", "kernel.verbosity", choice("quiet", "normal", "verbose"), on_context=True),
    TurnSetting("kernel_render_max_lines", "kernel.render_max_lines", integer, on_context=True),
    TurnSetting("kernel_wait_seconds", "kernel.wait_seconds", integer, on_context=True),
    TurnSetting("shell_wait_seconds", "shell.wait_seconds", integer, on_context=True),
    TurnSetting("shell_program", "shell.program", text, on_context=True),
    TurnSetting("max_parallel_tools", "runtime.max_parallel_tools", at_least_one, on_context=True),
    TurnSetting("jail_bind", "jail.bind", paths, on_context=True),
    TurnSetting("lsp_servers", "lsp.servers", entries, on_context=True),
    TurnSetting("lsp_timeout_s", "lsp.timeout_s", integer, on_context=True),
    TurnSetting("notebook_output_lines", "notebook.output_lines", integer, on_context=True),
    # --- on the Config only ---
    TurnSetting("max_tool_iterations", "limits.max_tool_iterations", integer),
    TurnSetting("max_tool_calls_per_message", "limits.max_tool_calls_per_message", integer),
    TurnSetting("max_tool_results_per_turn_bytes", "limits.max_tool_results_per_turn_bytes", integer),
    TurnSetting("inline_code_timeout_s", "limits.inline_code_timeout_s", integer),
    TurnSetting("max_text_attachment_bytes", "limits.max_text_attachment_bytes", integer),
    TurnSetting("max_output_tokens", "model.max_output_tokens", optional_integer),
    TurnSetting("model_context_window", "model.context_window", optional_integer),
    TurnSetting("thinking_budget", "model.thinking_budget", optional_integer),
    TurnSetting("trace", "runtime.trace", flag),
    TurnSetting("prefer_inherit", "subagents.prefer_inherit", flag),
    TurnSetting("lock_subagent_model", "subagents.lock_model", flag),
    TurnSetting("allow_inline_code", "runtime.allow_inline_code", flag),
    TurnSetting("debug_autolog", "runtime.debug_autolog", flag),
    TurnSetting("debug_autolog_dir", "runtime.debug_autolog_dir", optional_text),
    TurnSetting("transcript_log", "runtime.transcript_log", flag),
    TurnSetting("transcript_log_dir", "runtime.transcript_log_dir", optional_text),
)

_MISSING = object()


def project(settings: dict | None, fallback: object | None = None) -> dict[str, Any]:
    """attr -> effective value for every row, read from ``settings``. A value
    the store does not hold, or holds in a form the row cannot read, is
    ``fallback``'s attribute when a fallback is given, else the js/jsrc value."""
    store = settings if isinstance(settings, dict) else {}
    values: dict[str, Any] = {}
    for row in TURN_SETTINGS:
        raw = _settings.get_dotted(store, tuple(row.key.split(".")), _MISSING)
        value = INVALID if raw is _MISSING else row.read(raw)
        if value is INVALID:
            value = getattr(fallback, row.attr) if fallback is not None else row.default()
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
