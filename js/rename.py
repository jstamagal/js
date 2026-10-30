"""Moving files from the old setting names to the new ones.

`js --migrate-config` (`renamed_key`, `renamed_value`) and
scripts/rename-settings.py use it, and config load rewrites each jsrc it
loads with it once (`migrate_jsrc_file`); `set`, jsrc and --extra take only
the new names.

Only a jsrc file is rewritten (`kind_of`): a `jsrc`, a `.jsrc*`, an `.irc`,
and the jsrc `js --migrate-config` writes. `rename_old_names` changes such a
file only where it can tell what each word is:

- the setting name right after a `set` / `set -` command word at the start
  of a line (commented `#set` and `# example: set` lines included), or of an
  `alias` / `on` body that is exactly one `set NAME VALUE...` with no shell
  operator, redirection or newline in it. Any other body is only reported on.

Values are never touched, with one exception: history.cwd_first's boolean
literal becomes HISTORY_ORDER's `cwd` or `last` in the same quotes; any other
value there leaves the line as it was. An active `set` of a deleted setting
is commented out. A line that cannot be read is left as it was. Whatever the
outcome, a line that still holds an old setting name, a deleted setting's
name or an old env name anywhere, comments and quotes included, is reported
for a hand edit (`line_report`, which reads it as written, without
backslashes, without quote characters, without both and as a shell reads
its words); so
is every line setting one new name from two spellings,
since renaming changed which of them wins.

Any other file, a shell script or a Python program, is never rewritten:
`report_old_names` lists every old setting name, old env var name and
deleted name the same `line_report` finds in it, and those callers are
edited by hand.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

from . import messages as msgs
from . import output
from .settings import SPEC_BY_KEY, _locked_jsrc, parse_bool

# The names settings had before they were UPPER_SNAKE. `set`, jsrc and
# --extra take only the new names.
RENAMED_SETTINGS: dict[str, str] = {
    "agent": "DEFAULT_AGENT",
    # How js looks: /fset entries now, set with `fset`.
    "ui.reasoning": "REASONING_DISPLAY",
    "ui.net": "NET_DISPLAY",
    "ui.tools": "TOOLS_DISPLAY",
    "ui.tools_quiet": "TOOLS_QUIET",
    "ui.shell_quiet": "SHELL_QUIET",
    "ui.tools_preview_lines": "TOOLS_PREVIEW_LINES",
    "ui.markdown": "MARKDOWN",
    "ui.resume_exchanges": "RESUME_EXCHANGES",
    "runtime.trace": "TRACE",
    "kernel.verbosity": "KERNEL_VERBOSITY",
    "kernel.render_max_lines": "KERNEL_LINES_MAX",
    "ui.status_bg": "COLOR_STATUS_BACKGROUND",
    "ui.status_fg": "COLOR_STATUS_TEXT",
    "theme.accent": "COLOR_TOOL",
    "theme.ok": "COLOR_OK",
    "theme.error": "COLOR_ERROR",
    "theme.warn": "COLOR_WARN",
    "theme.dim": "COLOR_DIM",
    "theme.panel": "COLOR_PANEL",
    "theme.panel_text": "COLOR_TOOL_OUTPUT",
    "theme.added": "COLOR_ADDED",
    "theme.removed": "COLOR_REMOVED",
    "theme.code": "COLOR_CODE",
    "model.id": "MODEL",
    "model.max_output_tokens": "OUTPUT_TOKENS_MAX",
    "model.context_window": "CONTEXT_SIZE",
    "model.reasoning_effort": "EFFORT",
    "model.thinking_budget": "THINKING_BUDGET",
    "model.vision": "VISION",
    "history.file": "HISTORY_FILE",
    "history.cwd_first": "HISTORY_ORDER",
    "history.max_entries": "HISTORY_SIZE",
    "keys.file": "KEYBINDS_FILE",
    "ui.editing_mode": "EDITING_MODE",
    "ui.paste_image_key": "PASTE_IMAGE_KEY",
    "ui.paste_collapse_lines": "PASTE_COLLAPSE_LINES",
    "ui.paste_collapse_chars": "PASTE_COLLAPSE_CHARS",
    "ui.paste_image_command": "PASTE_IMAGE_COMMAND",
    "provider.id": "PROVIDER",
    "provider.base_url": "BASE_URL",
    "provider.api_key": "API_KEY",
    "provider.extra": "EXTRA_PARAMS",
    "limits.max_tool_iterations": "TURN_ROUNDS_MAX",
    "limits.max_tool_calls_per_message": "REPLY_TOOL_CALLS_MAX",
    "limits.max_bash_output_bytes": "SHELL_OUTPUT_MAX",
    "limits.max_tool_result_inline_bytes": "TOOL_RESULT_INLINE_MAX",
    "limits.max_tool_result_bytes": "TOOL_RESULT_MAX",
    "limits.max_tool_results_per_turn_bytes": "PARALLEL_RESULTS_MAX",
    "limits.fetch_timeout_s": "FETCH_TIMEOUT",
    "limits.browse_timeout_s": "BROWSE_TIMEOUT",
    "limits.download_timeout_s": "DOWNLOAD_TIMEOUT",
    "limits.max_download_bytes": "DOWNLOAD_MAX",
    "limits.inline_code_timeout_s": "INLINE_CODE_TIMEOUT",
    "limits.shell_env_allow": "SHELL_ENV_KEEP",
    "limits.max_read_lines": "READ_LINES_MAX",
    "limits.max_read_bytes": "READ_BYTES_MAX",
    "limits.max_file_bytes": "FILE_BYTES_MAX",
    "limits.max_text_attachment_bytes": "ATTACHMENT_MAX",
    "limits.task_max_depth": "SUBAGENT_DEPTH_MAX",
    "limits.subagent_max_workers": "SUBAGENT_WORKERS_MAX",
    "shell.program": "SHELL_PROGRAM",
    "shell.wait_seconds": "SHELL_WAIT",
    "kernel.wait_seconds": "PYTHON_KERNEL_WAIT",
    "swarm.slots": "SWARM_ACTIVE_MAX",
    "jail.bind": "JAIL_BIND",
    "runtime.max_parallel_tools": "PARALLEL_TOOLS_MAX",
    "runtime.retry_attempts": "RETRY_COUNT_MAX",
    "runtime.retry_max_wait_seconds": "RETRY_WAIT_MAX",
    "runtime.stream_idle_seconds": "STREAM_IDLE_TIMEOUT",
    "runtime.max_output_escalation": "OUTPUT_TOKENS_RETRY_MAX",
    "runtime.max_output_resumes": "OUTPUT_RESUMES_MAX",
    "runtime.steer": "STEER_MODE",
    "runtime.debug": "DEBUG_LOG",
    "runtime.debug_autolog": "REQUEST_LOG",
    "runtime.debug_autolog_dir": "REQUEST_LOG_DIR",
    "runtime.transcript_log": "TRANSCRIPT_LOG",
    "runtime.transcript_log_dir": "TRANSCRIPT_LOG_DIR",
    "runtime.allow_inline_code": "INLINE_CODE",
    "events.exec_timeout_s": "EXEC_TIMEOUT",
    "events.exec_output_bytes": "EXEC_OUTPUT_MAX",
    "compact.auto": "COMPACTION_AUTO",
    "compact.model": "COMPACTION_EXTERNAL_MODEL",
    "compact.chars_per_token": "COMPACTION_CHARS_PER_TOKEN",
    "compact.cache_ttl_seconds": "COMPACTION_CACHE_TTL",
    "compact.notify_threshold": "COMPACTION_WARN_AT",
    "compact.trigger_threshold": "COMPACTION_START_AT",
    "compact.force_threshold": "COMPACTION_FORCE_AT",
    "compact.buffer_tokens": "COMPACTION_HEADROOM_TOKENS",
    "compact.summary_reserve_tokens": "COMPACTION_REPLY_RESERVE_TOKENS",
    "compact.tail_tokens": "COMPACTION_KEEP_RECENT_TOKENS",
    "compact.min_savings_tokens": "COMPACTION_MIN_SAVINGS_TOKENS",
    "compact.clear_keep_recent": "COMPACTION_KEEP_TOOL_RESULTS",
    "compact.rehydrate_max_files": "COMPACTION_REATTACH_FILES_MAX",
    "compact.rehydrate_token_budget": "COMPACTION_REATTACH_TOKENS_MAX",
    "compact.rehydrate_max_tokens_per_file": "COMPACTION_REATTACH_FILE_TOKENS_MAX",
    "compact.summary_max_tokens": "COMPACTION_SUMMARY_TOKENS_MAX",
    "compact.summary_tool_result_chars": "COMPACTION_SUMMARY_RESULT_CHARS_MAX",
    "compact.max_summary_failures": "COMPACTION_FAILURES_MAX",
    "compact.pre_hook": "COMPACTION_HINT_COMMAND",
    "compact.flight_log_dir": "COMPACTION_LOG_DIR",
    "compact.context_window_overrides": "CONTEXT_SIZE_PER_MODEL",
    "compact.context_window_fallback": "CONTEXT_SIZE_UNKNOWN",
    "subagents.prefer_inherit": "SUBAGENT_INHERIT_MODEL",
    "subagents.lock_model": "SUBAGENT_MODEL_LOCK",
    "tools.alias_profiles": "TOOL_ALIASES",
    "tools.user_agent": "HTTP_USER_AGENT",
    "tools.terminal_cols": "TERMINAL_COLUMNS",
    "tools.terminal_rows": "TERMINAL_ROWS",
    "lsp.servers": "LSP_SERVERS",
    "lsp.timeout_s": "LSP_TIMEOUT",
    "notebook.output_lines": "NOTEBOOK_OUTPUT_LINES_MAX",
    "mcp.servers": "MCP_SERVERS",
    "mcp.agents": "MCP_AGENT_POLICY",
    "mcp.request_timeout_s": "MCP_TIMEOUT",
    "sampling.temperature": "TEMPERATURE",
    "sampling.top_p": "TOP_P",
    "sampling.top_k": "TOP_K",
    "sampling.repetition_penalty": "REPETITION_PENALTY",
    "sampling.presence_penalty": "PRESENCE_PENALTY",
}

# Settings that were removed, not renamed.
DELETED_SETTINGS: tuple[str, ...] = ("compact.context_window", "limits.max_bash_output_ceiling")

# Old env var names and the ones that replaced them. js itself still reads
# the three that `SettingSpec.env` names.
RENAMED_ENV: dict[str, str] = {
    "JS_AGENT": "JS_DEFAULT_AGENT",
    "JS_REASONING": "JS_EFFORT",
    "JS_CONTEXT_WINDOW": "JS_CONTEXT_SIZE",
    "JS_MAX_OUTPUT_TOKENS": "JS_OUTPUT_TOKENS_MAX",
    "JS_ALLOW_INLINE_CODE": "JS_INLINE_CODE",
    "JS_TEMP": "JS_TEMPERATURE",
    "JS_TOPP": "JS_TOP_P",
    "JS_TOPK": "JS_TOP_K",
    "JS_REPPEN": "JS_REPETITION_PENALTY",
    "JS_PRPEN": "JS_PRESENCE_PENALTY",
    "JS_DEBUG": "JS_DEBUG_LOG",
    "JS_DEBUG_AUTOLOG": "JS_REQUEST_LOG",
    "JS_DEBUG_AUTOLOG_DIR": "JS_REQUEST_LOG_DIR",
    "JS_MAX_TOOL_ITERATIONS": "JS_TURN_ROUNDS_MAX",
    "JS_MAX_TOOL_CALLS_PER_MESSAGE": "JS_REPLY_TOOL_CALLS_MAX",
    "JS_MAX_BASH_OUTPUT_BYTES": "JS_SHELL_OUTPUT_MAX",
    "JS_MAX_TOOL_RESULT_BYTES": "JS_TOOL_RESULT_MAX",
    "JS_MAX_DOWNLOAD_BYTES": "JS_DOWNLOAD_MAX",
}


def _canonical_env(key: str) -> str:
    """The env var main read for a dotted setting: ``a.b`` -> ``JS_A_B``."""
    return "JS_" + key.upper().replace(".", "_")


# Main also read every setting from JS_<DOTTED_NAME>: JS_COMPACT_AUTO,
# JS_LIMITS_MAX_READ_LINES. Each becomes the JS_<NAME> of its new setting.
# A colour entry (`COLOR_TOOL`) is no setting and has no env var.
for _old, _new in RENAMED_SETTINGS.items():
    if "." in _old and _new in SPEC_BY_KEY and _canonical_env(_old) != "JS_" + _new:
        RENAMED_ENV.setdefault(_canonical_env(_old), "JS_" + _new)

# Env names of deleted settings and of colour entries: reported, never renamed.
DELETED_ENV: tuple[str, ...] = tuple(_canonical_env(key) for key in DELETED_SETTINGS) + tuple(
    _canonical_env(old) for old, new in RENAMED_SETTINGS.items() if "." in old and new not in SPEC_BY_KEY)


def renamed_key(old: str) -> str | None:
    """The new name of old setting name ``old``, or None. A child of a renamed
    map setting keeps its child key, whatever characters it holds:
    compact.context_window_overrides.openai/gpt-6 is
    CONTEXT_SIZE_PER_MODEL.openai/gpt-6."""
    if old in RENAMED_SETTINGS:
        return RENAMED_SETTINGS[old]
    for prefix, new in RENAMED_SETTINGS.items():
        spec = SPEC_BY_KEY.get(new)
        if spec is not None and spec.type == "map" and old.startswith(prefix + "."):
            return new + old[len(prefix):]
    return None


def renamed_value(old_key: str, value: str) -> str:
    """``value`` of old setting ``old_key`` as its new setting takes it. Only
    history.cwd_first changes: a boolean literal on/off is HISTORY_ORDER
    cwd/last; anything else is left as it is."""
    if old_key == "history.cwd_first":
        flag = parse_bool(value)
        if flag is not None:
            return "cwd" if flag else "last"
    return value


# ---------------------------------------------------------------------------
# Words of a line
# ---------------------------------------------------------------------------

class Unparsable(ValueError):
    """The line cannot be migrated safely: a quote that is never closed, or
    a history.cwd_first value that is not a boolean literal."""


def words(line: str, start: int = 0) -> Iterator[tuple[int, int]]:
    """(start, end) of each shell word of ``line`` from ``start``: split on
    whitespace, a quoted run is part of its word, a backslash escapes the
    next character. Raises Unparsable for a quote that is not closed when
    the word holding it is reached."""
    i, n = start, len(line)
    while True:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            return
        begin = i
        while i < n and not line[i].isspace():
            char = line[i]
            if char == "\\":
                i += 2
                continue
            if char in "\"'":
                close = line.find(char, i + 1)
                while char == '"' and close > 0 and line[close - 1] == "\\":
                    close = line.find(char, close + 1)
                if close < 0:
                    raise Unparsable(line)
                i = close + 1
                continue
            i += 1
        yield begin, min(i, n)


def _unquoted(raw: str) -> tuple[str, str]:
    """(quote, inner) for a word wholly in one pair of quotes, else ("", raw)."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'" and raw[0] not in raw[1:-1]:
        return raw[0], raw[1:-1]
    return "", raw


# ---------------------------------------------------------------------------
# set lines
# ---------------------------------------------------------------------------

# Where a jsrc command starts on a commented line: `#set X` or `# example: set X`.
_COMMENTED = re.compile(r"#\s*(?:example:\s*)?(?=/?set\s)", re.IGNORECASE)


def _command_start(line: str) -> int | None:
    """Where the jsrc command of ``line`` starts, or None for prose."""
    body = len(line) - len(line.lstrip())
    if line.startswith("#", body):
        match = _COMMENTED.match(line, body)
        return match.end() if match else None
    return body


def _rename_set(line: str, start: int, assigns: list[tuple[str, str]] | None = None) -> str:
    """``line`` with the setting name of the `set` command at ``start``
    renamed, or of the set command an `alias` or `on` body there holds.
    A top-level `set NAME VALUE` adds (new name, name as written) to
    ``assigns``."""
    it = words(line, start)
    first = next(it, None)
    if first is None:
        return line
    verb = line[first[0]:first[1]].lstrip("/").lower()
    command = first
    if verb in ("alias", "on"):
        if next(it, None) is None:
            return line
        body = next(it, None)
        if body is not None and line[body[0]:body[1]] == "=":
            body = next(it, None)
        if body is None or line[body[0]:body[1]].lstrip("/").lower() != "set":
            return line
        command = body
    elif verb != "set":
        return line
    name = next(it, None)
    if name is None:
        return line
    raw = line[name[0]:name[1]]
    dash = "-" if raw.startswith("-") else ""
    old = raw[len(dash):]
    new = renamed_key(old)
    if assigns is not None and verb == "set" and not dash:
        assigns.append(((new or old).upper(), old if new else old.upper()))
    if new is None:
        return line
    out = line[:name[0]] + dash + new
    if is_fset(new):
        # The command word `set` becomes `fset`.
        out = line[:command[1] - 3] + "f" + line[command[1] - 3:name[0]] + dash + new
    value = next(it, None) if old == "history.cwd_first" and not dash else None
    if value is None:
        return out + line[name[1]:]
    quote, inner = _unquoted(line[value[0]:value[1]])
    return out + line[name[1]:value[0]] + quote + _literal_value(old, inner) + quote + line[value[1]:]


def _literal_value(old: str, value: str) -> str:
    """`renamed_value`, refusing a history.cwd_first value that is not a
    boolean literal (`$X`, a backtick command): its new form cannot be known."""
    if old == "history.cwd_first" and parse_bool(value) is None:
        raise Unparsable(value)
    return renamed_value(old, value)


def is_fset(new: str) -> bool:
    """Whether new name ``new`` is an /fset entry, set with `fset`."""
    spec = SPEC_BY_KEY.get(new)
    return new.startswith("COLOR_") or (spec is not None and spec.command == "fset")


# ---------------------------------------------------------------------------
# A jsrc file: rewritten
# ---------------------------------------------------------------------------

_OLD_ENV = "(" + "|".join(re.escape(key) for key in sorted(RENAMED_ENV, key=len, reverse=True)) + ")"
_OLD_DOTTED = sorted((key for key in RENAMED_SETTINGS if "." in key), key=len, reverse=True)
_LEFT_SETTING = re.compile(r"(?<![\w.])(" + "|".join(re.escape(key) for key in _OLD_DOTTED) + r")(?![\w])")
_LEFT_ENV = re.compile(r"\b" + _OLD_ENV + r"\b")
# An undotted old name (`agent`) is a common word: it counts only where a
# setting name goes, a `KEY=` word (quoted or not) or the name after `set`.
_OLD_UNDOTTED = "(" + "|".join(re.escape(key) for key in RENAMED_SETTINGS if "." not in key) + ")"
_LEFT_UNDOTTED = re.compile(r"(?<![\w.-])[\"']?" + _OLD_UNDOTTED + r"[\"']?=|\bset\s+-?[\"']?"
                            + _OLD_UNDOTTED + r"\b", re.IGNORECASE)
# An undotted old name as a whole quoted string (`"agent"`) is reported too:
# it may be a settings key.
_QUOTED_UNDOTTED = re.compile(r"([\"'])" + _OLD_UNDOTTED + r"\1", re.IGNORECASE)
# Deleted settings: the two removed with the rename, and session tagging.
_DELETED_NAME = r"(?:" + "|".join(re.escape(key) for key in DELETED_SETTINGS) + r"|tags\.[a-z_]+)"
_LEFT_DELETED = re.compile(r"(?<![\w.])(" + _DELETED_NAME + r")(?![\w])|\b("
                           + "|".join(DELETED_ENV) + r"|JS_TAGS_[A-Z_]+)\b")
_DELETED = re.compile(r"\s*/?set\s+-?" + _DELETED_NAME + r"(?=\s|$)", re.IGNORECASE)


def is_deleted(key: str) -> bool:
    """Whether ``key`` names a deleted setting or a child of one."""
    return _LEFT_DELETED.match(key) is not None


# Text that makes a body more than one plain command, attached or not:
# a list or pipe, a redirection, a command substitution, a newline.
_BODY_OPERATORS = re.compile(r"[;&|<>`\n]|\$\(")


def _rename_body(line: str, start: int) -> str:
    """An `alias` / `on` line at ``start`` with its body renamed only when the
    body is exactly one `set NAME VALUE...` with no shell operator, redirection or newline:
    then NAME is renamed (`_rename_set`). Any other body is left for the
    catch-all to report."""
    body = [line[begin:end] for begin, end in words(line, start)][2:]
    if body and body[0] == "=":
        body = body[1:]
    if not body or body[0].lstrip("/").lower() != "set" or _BODY_OPERATORS.search(line, start):
        return line
    return _rename_set(line, start)


def kind_of(path: str) -> str:
    """"jsrc" for a jsrc file (`jsrc*`, `.jsrc*`, `*.irc`), which is
    rewritten; "other" for anything else, which is only reported on."""
    name = path.rsplit("/", 1)[-1]
    return "jsrc" if name.startswith(("jsrc", ".jsrc")) or name.endswith(".irc") else "other"


def _migrated(line: str, assigns: list[tuple[str, str]]) -> str:
    """jsrc ``line`` with every setting name it can rename safely renamed;
    unchanged when it cannot be read."""
    start = _command_start(line)
    if start is None:
        return line
    try:
        first = next(words(line, start), None)
        verb = line[first[0]:first[1]].lstrip("/").lower() if first else ""
        active = not line.lstrip().startswith("#")
        if verb == "set" and active and _DELETED.match(line, start):
            return "#" + line
        if verb == "set":
            return _rename_set(line, start, assigns if active else None)
        if verb in ("alias", "on"):
            return _rename_body(line, start)
    except Unparsable:
        pass
    return line


_QUOTES = str.maketrans("", "", "'\"`")
_EQUALS = re.compile(r"\s*=\s*")


def _views(line: str) -> list[str]:
    """The ways ``line`` is read for old names: as written, with every
    backslash removed (`model\\.id`), with every quote character removed
    (`ag'ent'`), with both removed (`model'.'\\id`), and as a shell reads its
    words when it can (a line it cannot read, an apostrophe in a comment,
    loses only that view). In each, whitespace around `=` is dropped, so
    `agent = x` reads as `agent=x`."""
    unescaped = line.replace("\\", "")
    views = [line, unescaped, line.translate(_QUOTES), unescaped.translate(_QUOTES)]
    try:
        views.append(" ".join(shlex.split(line, comments=False, posix=True)))
    except ValueError:
        pass
    return [_EQUALS.sub("=", view) for view in views]


def line_report(line: str) -> list[tuple[str, str | None]]:
    """(old name, new name) for each old setting name, old env var name and
    deleted name (new name None) in any view of ``line`` (`_views`). The one
    reporter every file kind uses: the jsrc catch-all and the report on a
    shell script or a Python program. An undotted old name (`agent`) counts
    where a setting name goes (`KEY=`, after `set`) or as a whole quoted
    string."""
    found: list[tuple[str, str | None]] = []
    for view in _views(line):
        hits: list[tuple[int, str, str | None]] = []
        deleted = {match.start() for match in _LEFT_DELETED.finditer(view)}
        hits += [(match.start(), match.group(0), None) for match in _LEFT_DELETED.finditer(view)]
        hits += [(match.start(), match.group(1), renamed_key(match.group(1)))
                 for match in _LEFT_SETTING.finditer(view) if match.start() not in deleted]
        for match in _LEFT_UNDOTTED.finditer(view):
            name = match.group(1) or match.group(2)
            hits.append((match.start(), name, RENAMED_SETTINGS[name.lower()]))
        hits += [(match.start(), match.group(2), RENAMED_SETTINGS[match.group(2).lower()])
                 for match in _QUOTED_UNDOTTED.finditer(view)]
        hits += [(match.start(), match.group(1), RENAMED_ENV[match.group(1)]) for match in _LEFT_ENV.finditer(view)]
        for _start, old, new in sorted(hits):
            if (old, new) not in found:
                found.append((old, new))
    return found


def migrate_line(line: str, assigns: list[tuple[str, str]] | None = None) -> tuple[str, str | None]:
    """jsrc ``line`` migrated, and None when it is done, else what to report:
    "" for an old name still on the line, " (setting deleted)" for a deleted
    setting's name, " (set twice)" when the line now sets one name twice
    from two spellings. Anything old left anywhere on the result, in a
    value, a quote or a comment, is reported."""
    found: list[tuple[str, str]] = []
    new = _migrated(line, found)
    if assigns is not None:
        assigns.extend(found)
    left = line_report(new)
    if any(name is None for _old, name in left):
        return new, " (setting deleted)"
    if left:
        return new, ""
    if _twice(found):
        return new, " (set twice)"
    return new, None


def _twice(assigns: list[tuple[str, str]]) -> set[str]:
    """The new names ``assigns`` sets from more than one spelling, where
    renaming changed which assignment wins."""
    spellings: dict[str, set[str]] = {}
    for new, old in assigns:
        spellings.setdefault(new, set()).add(old)
    return {new for new, olds in spellings.items() if len(olds) > 1}


def rename_old_names(text: str) -> tuple[str, list[tuple[int, str]]]:
    """jsrc ``text`` with old setting names replaced (module docstring), and
    (line number, note) for each line left for a hand edit. Lines that set
    one new name from two spellings, old and new or two old ones, are all
    reported: renaming changed which of them wins."""
    out: list[str] = []
    notes: dict[int, str] = {}
    by_line: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.split("\n"), 1):
        found: list[tuple[str, str]] = []
        new, note = migrate_line(line, found)
        out.append(new)
        by_line.extend((number, name, old) for name, old in found)
        if note is not None:
            notes[number] = note
    twice = _twice([(name, old) for _number, name, old in by_line])
    for number, name, _old in by_line:
        if name in twice:
            notes.setdefault(number, " (set twice)")
    return "\n".join(out), sorted(notes.items())


# ---------------------------------------------------------------------------
# A jsrc js loads: rewritten in place once
# ---------------------------------------------------------------------------

# What starts a live jsrc line `migrate_jsrc_text` could not convert, now a
# comment.
NOT_MIGRATED = msgs.JSRC_NOT_MIGRATED.format()

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rename-settings.py"

# The files this process already warned it cannot write.
_UNWRITABLE_WARNED: set[Path] = set()


# A `set` of an old or deleted setting name where a command starts: the line
# start, after a shell operator, or an `alias` / `on` body.
_OLD_COMMAND = re.compile(
    r"(?:^|[;&|(`]|\b(?:alias|on)\s+\S+(?:\s+=)?)\s*/?set\s+-?[\"']?(?:"
    + "|".join(re.escape(key) for key in sorted(RENAMED_SETTINGS, key=len, reverse=True))
    + "|" + _DELETED_NAME + r")(?!\w)", re.IGNORECASE)


def migrate_jsrc_text(text: str) -> tuple[str, list[int]]:
    """jsrc ``text`` as `rename_old_names` rewrites it, and the numbers of the
    live lines it reports that are commented out behind NOT_MIGRATED as they
    were written: a line the rename made a comment (a deleted setting), a
    line that still sets an old name (`_OLD_COMMAND`), and an old spelling
    of a name the file also sets by its new one, so what loads is what the
    new spelling sets. A reported line that only mentions an old name, in a
    value or a body that sets nothing, stays live as renamed, and a comment
    stays as it is: they set nothing old."""
    new, notes = rename_old_names(text)
    old_lines = text.split("\n")
    new_lines = new.split("\n")
    assigns = [(number, name, old) for number, line in enumerate(old_lines, 1)
               for name, old in _assigns(line)]
    twice = _twice([(name, old) for _number, name, old in assigns])
    old_spelling = {number for number, name, old in assigns if name in twice and old != name}
    commented: list[int] = []
    for number, note in notes:
        line = old_lines[number - 1]
        renamed = new_lines[number - 1]
        if line.lstrip().startswith("#"):
            continue
        if renamed.lstrip().startswith("#") or _OLD_COMMAND.search(renamed) or number in old_spelling:
            new_lines[number - 1] = f"{NOT_MIGRATED}{note}: {line}"
            commented.append(number)
    return "\n".join(new_lines), commented


def _assigns(line: str) -> list[tuple[str, str]]:
    """(new name, name as written) for the top-level `set` of jsrc ``line``."""
    found: list[tuple[str, str]] = []
    _migrated(line, found)
    return found


def _command(path: Path) -> str:
    """The command that rewrites ``path`` by hand."""
    return f"{shlex.quote(str(_SCRIPT))} --write {shlex.quote(str(path))}"


def _create(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to a new file at ``path`` with permissions ``mode``,
    synced to disk. Raises FileExistsError when anything is at ``path``; no
    one but the owner can read the file before it has ``mode``."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _notice(message: msgs.Message, stream: TextIO, **fields) -> None:
    output.emit(output.event("migration_notice", message=message, fields=fields, stream=stream))


def migrate_jsrc_file(path: Path, out: TextIO | None = None) -> bool:
    """Rewrite the jsrc at ``path`` in place by `migrate_jsrc_text` when that
    changes it. Prints one line naming the file, and one warning naming the
    lines it commented out. A file or directory js cannot write is left as it is, with
    one warning per process naming the command to run by hand. Returns
    whether the file was rewritten."""
    stream = out if out is not None else sys.stderr
    try:
        path = path.resolve(strict=False)
        with _locked_jsrc(path):
            return _migrate_jsrc_file(path, stream)
    except OSError:
        return False


def _migrate_jsrc_file(path: Path, stream: TextIO) -> bool:
    try:
        old = path.read_bytes()
        text = old.decode("utf-8")
    except (OSError, UnicodeError):
        return False
    new, commented = migrate_jsrc_text(text)
    if new == text:
        return False
    if not (os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)):
        if path not in _UNWRITABLE_WARNED:
            _UNWRITABLE_WARNED.add(path)
            _notice(msgs.JSRC_NOT_WRITABLE, stream, path=path, command=_command(path))
        return False
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        temp.unlink(missing_ok=True)
        _create(temp, new.encode("utf-8"), mode)
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)
        _notice(msgs.JSRC_NOT_WRITABLE, stream, path=path, command=_command(path))
        return False
    _notice(msgs.JSRC_RENAMED, stream, path=path)
    if commented:
        _notice(msgs.JSRC_NOT_RENAMED, stream, path=path, lines=",".join(map(str, commented)))
    return True


# ---------------------------------------------------------------------------
# Any other file: reported, never rewritten
# ---------------------------------------------------------------------------

def report_old_names(text: str) -> list[tuple[int, str, str | None]]:
    """(line number, old name, new name) for every old name `line_report`
    finds in ``text``, the new name None for a deleted one. Nothing is
    rewritten: a shell script or a Python file is edited by hand from this
    list."""
    return [(number, old, new) for number, line in enumerate(text.split("\n"), 1)
            for old, new in line_report(line)]
