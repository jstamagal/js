"""What a setting's value can be: text read into a value for a setting
(`coerce_value`), shared by `set`, the env layer and `--extra`, and a value
spelled as a jsrc line holds it (`config_value_text`, `jsrc_text`)."""

from __future__ import annotations

import json
import math
import re
import shlex
from typing import Any

from . import (
    fset,
    jail,
    jsrc_line,
    mcp_config,
    messages as msgs,
    reasoning as _reasoning,
    sampling as _sampling,
)
from .settings import SPEC_BY_KEY, SettingSpec, default_value

# What a line typed while a turn runs does: now = join the running turn at its
# next tool boundary, batch = one message after the turn, one = one turn per line.
STEER_MODES: tuple[str, ...] = ("now", "batch", "one")

# The order Up offers prompts in: last = newest first, cwd = this directory's first.
HISTORY_ORDERS: tuple[str, ...] = ("last", "cwd")


# ---------------------------------------------------------------------------
# Value coercion (shared by the env layer and the `set` command)
# ---------------------------------------------------------------------------

_TRUE_TOKENS = {"1", "true", "yes", "on"}
_FALSE_TOKENS = {"0", "false", "no", "off"}
_TOOL_ALIAS_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


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

# The effort words js knows: the ladder in js/reasoning.py, its bottom stop
# spelled "off". `EFFORT` takes any one word and stores it as typed; these
# are what completion offers.
REASONING_EFFORT_VALUES: tuple[str, ...] = ("off", *_reasoning.EFFORT_LADDER[1:])


def steer_mode(value: Any) -> str:
    """The STEER_MODE mode ``value`` names, or its defaults/jsrc value for anything else."""
    text = str(value or "").strip().lower()
    return text if text in STEER_MODES else default_value("STEER_MODE")


def parse_bool(raw: str) -> bool | None:
    v = raw.strip().lower()
    if v in _TRUE_TOKENS:
        return True
    if v in _FALSE_TOKENS:
        return False
    return None


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


_NONNEGATIVE_KNOBS = frozenset({
    "RETRY_COUNT_MAX", "RETRY_WAIT_MAX", "CONNECT_TIMEOUT", "FIRST_BYTE_TIMEOUT", "STREAM_IDLE_TIMEOUT",
    "OUTPUT_TOKENS_RETRY_MAX", "OUTPUT_RESUMES_MAX",
    "COMPACTION_HEADROOM_TOKENS", "COMPACTION_REPLY_RESERVE_TOKENS", "COMPACTION_KEEP_TOOL_RESULTS",
    "COMPACTION_OVERFLOW_KEEP_TOOL_RESULTS", "COMPACTION_REATTACH_FILES_MAX",
    "COMPACTION_REATTACH_TOKENS_MAX", "COMPACTION_REATTACH_FILE_TOKENS_MAX",
    "COMPACTION_SUMMARY_RESULT_CHARS_MAX", "COMPACTION_CACHE_TTL",
})

# Integer settings whose readers take only 1 and up.
_POSITIVE_INTEGERS = frozenset({
    "REPLY_TOOL_CALLS_MAX", "SUBAGENT_WORKERS_MAX", "TOOLS_PREVIEW_LINES", "KERNEL_LINES_MAX",
    "TERMINAL_COLUMNS", "TERMINAL_ROWS", "PARALLEL_TOOLS_MAX", "HISTORY_SIZE", "LSP_TIMEOUT",
    "CONTEXT_SIZE_UNKNOWN", "COMPACTION_KEEP_RECENT_TOKENS", "COMPACTION_MIN_SAVINGS_TOKENS",
    "COMPACTION_FAILURES_MAX", "COMPACTION_HINT_TIMEOUT", "COMPACTION_SUMMARY_TOKENS_MAX",
    "COMPACTION_SUMMARY_REQUEST_TOKENS_MAX", "COMPACTION_CONSECUTIVE_MAX",
})

# Fractions of the context window: above 0, at most 1.
_WINDOW_FRACTIONS = frozenset({"COMPACTION_WARN_AT", "COMPACTION_START_AT", "COMPACTION_FORCE_AT"})


# The values a sampling setting can take: (lowest, highest), None = open.
_SAMPLING_RANGES: dict[str, tuple[float | None, float | None]] = {
    "TEMPERATURE": (0.0, None),
    "TOP_P": (0.0, 1.0),
    "REPETITION_PENALTY": (0.0, None),
    "PRESENCE_PENALTY": (-2.0, 2.0),
    "MIN_P": (0.0, 1.0),
    "XTC_PROBABILITY": (0.0, 1.0),
    "XTC_THRESHOLD": (0.0, 1.0),
    "DRY_MULTIPLIER": (0.0, None),
    "DRY_BASE": (0.0, None),
}


def _unquoted(spec: SettingSpec, text: str) -> str:
    """A value that is not JSON as a jsrc line gives it: a JSON string
    literal, the form `/save` writes one a line cannot hold as it is, is the
    string it spells (`jsrc_line.from_line_literal`); any other text as it is."""
    return text if spec.type in ("json", "map") else jsrc_line.from_line_literal(text)


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
        if not text or len(text.split()) != 1:
            return None, msgs.EXPECTED_EFFORT_WORD.said(value=text)
        return text, None
    if spec.key == "TEMPLATE_KWARGS" and text:
        try:
            _sampling.parse_template_kwargs(text)
        except ValueError as exc:
            return None, str(exc)
        return text, None
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
    if spec.key in ("PASTE_IMAGE_COMMAND", "COPY_COMMAND"):
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
        if spec.key in _POSITIVE_INTEGERS and value < 1:
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
        if spec.key in {"MCP_TIMEOUT", "COMPACTION_CHARS_PER_TOKEN", *_WINDOW_FRACTIONS} and number <= 0:
            return None, msgs.EXPECTED_POSITIVE_NUMBER.said()
        if spec.key in _WINDOW_FRACTIONS and number > 1:
            return None, msgs.EXPECTED_RANGE.said(low=0, high=1)
        return number, None
    if kind in ("json", "map"):
        if spec.key in {"MCP_SERVERS", "MCP_AGENT_POLICY"}:
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
        if spec.key == "TOOL_PROFILES":
            from . import model_profiles

            try:
                model_profiles.parse_profiles(value, "TOOL_PROFILES")
            except ValueError as exc:
                return None, str(exc)
        if spec.key in {"SHELL_ENV_KEEP", "TOOLS_QUIET", "SHELL_QUIET"}:
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                return None, msgs.EXPECTED_ENV_NAMES.said()
        if spec.key == "DRY_SEQUENCE_BREAKERS":
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                return None, msgs.EXPECTED_STRING_LIST.said()
        if spec.key == "SKILLS_DISABLED":
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                return None, msgs.EXPECTED_SKILL_NAMES.said()
        if spec.key in {"JAIL_BIND", "JAIL_HIDE"}:
            try:
                (jail.parse_binds if spec.key == "JAIL_BIND" else jail.parse_hides)(value)
            except ValueError as exc:
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
# A value as a jsrc line spells it
# ---------------------------------------------------------------------------

def config_value_text(value: Any) -> str:
    """``value`` as `set` takes it: on/off, JSON for a list or a map, else
    the text — the inverse of `coerce_value`."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def jsrc_text(name: str, text: str) -> str:
    """``text`` as the value on a jsrc line for setting or /fset entry
    ``name``, so the line loads back to ``text`` (`jsrc_line.line_literal`): a `$` is
    written `$$`, since a jsrc line's `$NAME`s expand when it loads, except
    in a format or colour, which keeps its `$NAME`s for when it is drawn."""

    text = jsrc_line.line_literal(text)
    return text if fset.read_when_drawn(name) else text.replace("$", "$$")



def validate_cli_reasoning(reasoning: str) -> tuple[str | None, str | None]:
    """Check a raw ``--reasoning`` value through the same registry validation as
    ``set EFFORT`` and ``JS_REASONING``: any one word, kept as typed.
    Returns (value, error)."""
    spec = SPEC_BY_KEY["EFFORT"]
    return coerce_value(spec, reasoning)
