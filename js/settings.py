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
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Built-in defaults — the value used when no config file or env var supplies one.
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_MAX_TOOL_ITERATIONS = 50
DEFAULT_MAX_BASH_OUTPUT_BYTES = 256 * 1024
DEFAULT_MAX_BASH_OUTPUT_CEILING = 150_000
DEFAULT_MAX_TOOL_RESULT_INLINE_BYTES = 51_200
DEFAULT_MAX_TOOL_RESULT_BYTES = 256 * 1024
DEFAULT_FETCH_TIMEOUT_S = 15
DEFAULT_SHELL_ENV_ALLOW = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "PWD", "SHELL")
# browse drives a real browser engine: navigation, a JS event loop, and an
# adaptive settle wait. 15s is a plain-HTTP number and kills SPAs mid-render.
DEFAULT_BROWSE_TIMEOUT_S = 60
# A download is bounded by _DOWNLOAD_MAX_BYTES (32 MiB), not by page-load time.
# At 15s that silently demanded ~2 MB/s to fetch anything large.
DEFAULT_DOWNLOAD_TIMEOUT_S = 300
DEFAULT_MAX_DOWNLOAD_BYTES = 0        # 0 = unlimited; a download streams to disk
DEFAULT_INLINE_CODE_TIMEOUT_S = 300
DEFAULT_TRACE = True
DEFAULT_MAX_READ_LINES = 2_000
DEFAULT_MAX_LINE_CHARS = 2_000
DEFAULT_JSONL_MAX_LINE_CHARS = 65536
DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_MAX_READ_BYTES = 256 * 1024
DEFAULT_MAX_TOOL_RESULTS_PER_TURN_BYTES = 200_000
DEFAULT_TASK_MAX_DEPTH = 2
DEFAULT_SUBAGENT_MAX_WORKERS = 8
DEFAULT_KERNEL_VERBOSITY = "normal"
DEFAULT_KERNEL_RENDER_MAX_LINES = 24
DEFAULT_COMPACT_AUTO = True
DEFAULT_COMPACT_CONTEXT_WINDOW = None
DEFAULT_COMPACT_CONTEXT_WINDOW_FALLBACK = None
DEFAULT_COMPACT_NOTIFY_THRESHOLD = 0.50
DEFAULT_COMPACT_TRIGGER_THRESHOLD = 0.80
DEFAULT_COMPACT_FORCE_THRESHOLD = 0.90
DEFAULT_COMPACT_BUFFER_TOKENS = 4096
DEFAULT_COMPACT_SUMMARY_RESERVE_TOKENS = 20_000
DEFAULT_COMPACT_TAIL_TOKENS = 16384
DEFAULT_COMPACT_MIN_SAVINGS_TOKENS = 400
DEFAULT_COMPACT_CHARS_PER_TOKEN = 4.0
DEFAULT_COMPACT_MODEL = "same"
DEFAULT_COMPACT_SUMMARY_MAX_TOKENS = 8192


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
    SettingSpec("model.context_window", "int", None,
                "Override the active model's context window. Unset = server-reported "
                "limits, then models.dev metadata. Beats every other source; for a "
                "multi-model setup use compact.context_window_overrides instead.",
                env="JS_CONTEXT_WINDOW", empty=EMPTY_NONE),
    SettingSpec("model.reasoning_effort", "str", None,
                "Thinking effort: off|minimal|low|medium|high|xhigh|max (off disables); "
                "any other value is rejected. Clear with `set -model.reasoning_effort`.",
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
    SettingSpec("limits.max_bash_output_ceiling", "int", DEFAULT_MAX_BASH_OUTPUT_CEILING,
                "Upper bound a caller may raise max_bash_output_bytes to; the effective "
                "shell cap is min(max_bash_output_bytes, this)."),
    SettingSpec("limits.max_tool_result_inline_bytes", "int", DEFAULT_MAX_TOOL_RESULT_INLINE_BYTES,
                "Results larger than this are written to a file and replaced with a "
                "preview plus the path, instead of being clipped and lost. 0 = off."),
    SettingSpec("limits.max_tool_result_bytes", "int", DEFAULT_MAX_TOOL_RESULT_BYTES,
                "Hard cap on any tool result string.",
                env="JS_MAX_TOOL_RESULT_BYTES"),
    SettingSpec("limits.fetch_timeout_s", "int", DEFAULT_FETCH_TIMEOUT_S,
                "fetch() per-request timeout in seconds, and the timeout for the "
                "web-search backends' JSON calls.",
                env="JS_FETCH_TIMEOUT"),
    SettingSpec("limits.shell_env_allow", "json", list(DEFAULT_SHELL_ENV_ALLOW),
                "Environment-variable names inherited by shell() without naming "
                "them per call in env. The default is the eight-variable safe set; "
                "use a JSON string list to widen or narrow it."),
    SettingSpec("limits.browse_timeout_s", "int", DEFAULT_BROWSE_TIMEOUT_S,
                "browse() page budget in seconds. obscura is told to give up one "
                "second earlier so its own graceful navigation-timeout path runs "
                "and partial content survives.",
                env="JS_BROWSE_TIMEOUT"),
    SettingSpec("limits.download_timeout_s", "int", DEFAULT_DOWNLOAD_TIMEOUT_S,
                "aria2c transfer timeout in seconds for saved, binary, and oversized "
                "fetch() responses. Downloads are bounded by size, not by how fast a "
                "page renders.",
                env="JS_DOWNLOAD_TIMEOUT"),
    SettingSpec("limits.max_download_bytes", "int", DEFAULT_MAX_DOWNLOAD_BYTES,
                "Size ceiling for fetch(save=...) in bytes. 0 = unlimited, which is "
                "the default: a save streams to disk and never lands in memory, so "
                "an ISO or a model weight is a normal download. Set a number only to "
                "impose a quota.",
                env="JS_MAX_DOWNLOAD_BYTES"),
    SettingSpec("limits.inline_code_timeout_s", "int", DEFAULT_INLINE_CODE_TIMEOUT_S,
                "Timeout in seconds for !{sh|python|c|node ...} and ```!lang prompt expansions.",
                env="JS_INLINE_CODE_TIMEOUT"),
    SettingSpec("limits.max_read_lines", "int", DEFAULT_MAX_READ_LINES,
                "Maximum lines returned by read()."),
    SettingSpec("limits.max_line_chars", "int", DEFAULT_MAX_LINE_CHARS,
                "Maximum characters shown per read/search line."),
    SettingSpec("limits.jsonl_max_line_chars", "int", DEFAULT_JSONL_MAX_LINE_CHARS,
                "Maximum characters shown per read line for .jsonl files only.",
                env="JS_JSONL_MAX_LINE_CHARS"),
    SettingSpec("limits.max_file_bytes", "int", DEFAULT_MAX_FILE_BYTES,
                "Maximum file bytes read by fs tools."),
    SettingSpec("limits.max_read_bytes", "int", DEFAULT_MAX_READ_BYTES,
                "Maximum file bytes for a whole-file read(); ignored when the call "
                "passes start_line/end_line, so ranged reads work on any size file."),
    SettingSpec("limits.max_tool_results_per_turn_bytes", "int", DEFAULT_MAX_TOOL_RESULTS_PER_TURN_BYTES,
                "Aggregate cap on all tool results returned by one batch of parallel "
                "calls; the largest results are clipped first. 0 = unlimited."),
    SettingSpec("limits.task_max_depth", "int", DEFAULT_TASK_MAX_DEPTH,
                "Maximum recursive task/subagent depth."),
    SettingSpec("limits.subagent_max_workers", "int", DEFAULT_SUBAGENT_MAX_WORKERS,
                "Maximum concurrent subagent workers per task call; minimum 1."),
    # --- kernel ---
    SettingSpec("kernel.verbosity", "str", DEFAULT_KERNEL_VERBOSITY,
                "How much of each kernel/toolbox call is rendered to your terminal: "
                "quiet (errors and interrupts only), normal (code, output, timing, "
                "namespace), verbose (stdout/stderr/display split out, plus kernel "
                "lifecycle and toolbox activity). Affects only what you see; the model "
                "always receives the full result.",
                env="JS_KERNEL_VERBOSITY"),
    SettingSpec("kernel.render_max_lines", "int", DEFAULT_KERNEL_RENDER_MAX_LINES,
                "Line cap per section of that terminal render, so a 4000-line cell "
                "cannot scroll the screen away. The hidden count is always shown, and "
                "the model still gets the untrimmed output."),
    # --- runtime ---
    SettingSpec("runtime.debug", "bool", False,
                "Append per-event records to state/<agent>/debug.log.",
                env="JS_DEBUG", empty=EMPTY_OFF),
    SettingSpec("runtime.trace", "bool", DEFAULT_TRACE,
                "Pretty-print the tool-call trace line as the model runs.",
                env="JS_TRACE", empty=EMPTY_OFF),
    SettingSpec("runtime.debug_autolog", "bool", True,
                "Append the full request trace (unclipped system prompt, tool-schema "
                "JSON, and the messages sent each call) to logs/<agent>/<session>.log "
                "under the js data dir. On by default; this trace never prints to the "
                "terminal, only to the file.",
                env="JS_DEBUG_AUTOLOG", empty=EMPTY_OFF),
    SettingSpec("runtime.debug_autolog_dir", "str", None,
                "Directory for the debug autolog; unset = logs/<agent> under the js data dir.",
                env="JS_DEBUG_AUTOLOG_DIR", empty=EMPTY_NONE),
    SettingSpec("runtime.transcript_log", "bool", True,
                "Append the visible terminal/TUI transcript to transcript/<agent>/<session>.log "
                "under the js data dir. On by default; records what printed to the user with "
                "IRC-style <KING>/<APE> tags for user/assistant turns.",
                env="JS_TRANSCRIPT_LOG", empty=EMPTY_OFF),
    SettingSpec("runtime.transcript_log_dir", "str", None,
                "Directory for the visible transcript log; unset = transcript/<agent> under the js data dir.",
                env="JS_TRANSCRIPT_LOG_DIR", empty=EMPTY_NONE),
    SettingSpec("runtime.allow_inline_code", "bool", True,
                "Execute !{sh|python|c|node ...} inline directives / ```!lang fences in "
                "prompt files and inject their stdout. On by default (runs arbitrary code "
                "from prompt files); opt out with --im-a-pussy or set this off.",
                env="JS_ALLOW_INLINE_CODE", empty=EMPTY_OFF),
    # --- compact ---
    SettingSpec("compact.auto", "bool", DEFAULT_COMPACT_AUTO,
                "Automatic cache-aware context compaction.", empty=EMPTY_OFF),
    SettingSpec("compact.context_window", "int", DEFAULT_COMPACT_CONTEXT_WINDOW,
                "Context window tokens for fullness math; unset = models.dev metadata.",
                empty=EMPTY_NONE),
    SettingSpec("compact.context_window_overrides", "map", {},
                "Per-model context windows, keyed 'provider/model' (most specific) or "
                "'model'. For surfaces models.dev has no row for — a subscription "
                "endpoint serving the same model id as the public API with a different "
                "usable window.", empty=EMPTY_NONE),
    SettingSpec("compact.context_window_fallback", "int", DEFAULT_COMPACT_CONTEXT_WINDOW_FALLBACK,
                "Window to assume ONLY for models whose size cannot be resolved. Unlike "
                "context_window this does not override models that are known, so covering "
                "one unknown model no longer shrinks every known one.",
                empty=EMPTY_NONE),
    SettingSpec("compact.notify_threshold", "float", DEFAULT_COMPACT_NOTIFY_THRESHOLD,
                "Notify once when context reaches this fraction."),
    SettingSpec("compact.trigger_threshold", "float", DEFAULT_COMPACT_TRIGGER_THRESHOLD,
                "Auto-compact at this fullness fraction."),
    SettingSpec("compact.force_threshold", "float", DEFAULT_COMPACT_FORCE_THRESHOLD,
                "Force compact at this fullness fraction."),
    SettingSpec("compact.buffer_tokens", "int", DEFAULT_COMPACT_BUFFER_TOKENS,
                "Extra input-token headroom reserved by preflight/mid-turn compaction."),
    SettingSpec("compact.summary_reserve_tokens", "int", DEFAULT_COMPACT_SUMMARY_RESERVE_TOKENS,
                "Ceiling on the reply headroom subtracted before the fullness fractions; "
                "the actual reserve is min(model max_output_tokens, this). Stops a model "
                "declaring a 128k output cap from eating a third of the window."),
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
                "Model-facing tool-name alias profiles: list of {match:string|[...], aliases:{...}}.",
                empty=EMPTY_NONE),
    # --- mcp ---
    SettingSpec("mcp.servers", "json", {},
                "Named MCP servers as JSON: stdio uses command/args/env; streamable HTTP uses url/headers.",
                empty=EMPTY_NONE, secret=True),
    SettingSpec("mcp.agents", "json", {},
                "Per-agent MCP policy JSON with servers/tools allow and deny glob lists.",
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
)

SPEC_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in REGISTRY}
KNOWN_SECTIONS: frozenset[str] = frozenset(spec.section for spec in REGISTRY)
SECTION_ORDER: tuple[str, ...] = (
    "model",
    "provider",
    "limits",
    "kernel",
    "runtime",
    "compact",
    "subagents",
    "tools",
    "mcp",
    "sampling",
)


# ---------------------------------------------------------------------------
# Value coercion (shared by the env layer and the `set` command)
# ---------------------------------------------------------------------------

_TRUE_TOKENS = {"1", "true", "yes", "on"}
_FALSE_TOKENS = {"0", "false", "no", "off"}
_TOOL_ALIAS_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

# The only values `model.reasoning_effort` accepts. "off" disables reasoning
# (stored as the literal "none"); everything else is rejected outright — no
# default/auto/unset synonyms. Clearing the knob back to provider-default is
# `set -model.reasoning_effort`, never a magic value here.
REASONING_EFFORT_VALUES: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_REASONING_EFFORT_ERROR = "expected off|minimal|low|medium|high|xhigh|max"


def parse_bool(raw: str) -> bool | None:
    v = raw.strip().lower()
    if v in _TRUE_TOKENS:
        return True
    if v in _FALSE_TOKENS:
        return False
    return None


def coerce_value(spec: SettingSpec, raw: str) -> tuple[Any, str | None]:
    """Coerce ``raw`` for ``spec``. Returns (value, error). Values store
    VERBATIM — there is no magic clear-token (no "default"/"auto"/"none"/"unset"
    special-casing); the only way to clear a knob back to its default/unset
    state is `set -key` (see `apply_unset` in `js.setcmd`)."""
    text = raw.strip()
    if spec.key == "model.reasoning_effort":
        v = text.lower()
        if v not in REASONING_EFFORT_VALUES:
            return None, _REASONING_EFFORT_ERROR
        return ("none" if v == "off" else v), None
    if spec.key == "provider.id" and text:
        from . import providers as _providers

        if _providers.get_provider(text) is None:
            return None, (
                f"unknown provider id: {text!r} — pick a known id or add a "
                f"custom one with `js --login`"
            )
        return text, None
    if spec.key == "provider.base_url" and text:
        if not text.startswith(("http://", "https://")):
            return None, f"expected a URL starting with http:// or https:// (got {text!r})"
        return text, None
    kind = spec.type
    if kind == "bool":
        parsed = parse_bool(text)
        if parsed is None:
            return None, "expected on/off"
        return parsed, None
    if kind == "int":
        try:
            value = int(text)
        except ValueError:
            return None, "expected an integer"
        if spec.key == "limits.subagent_max_workers" and value < 1:
            return None, "expected an integer >= 1"
        return value, None
    if kind == "float":
        try:
            return float(text), None
        except ValueError:
            return None, "expected a number"
    if kind in ("json", "map"):
        if spec.key in {"mcp.servers", "mcp.agents"}:
            from . import mcp_config

            try:
                value = (
                    mcp_config.parse_servers_json(raw)
                    if spec.key == "mcp.servers"
                    else mcp_config.parse_agents_json(raw)
                )
            except mcp_config.MCPConfigError as exc:
                return None, str(exc)
        else:
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return None, "expected a JSON value"
        if kind == "map" and not isinstance(value, dict):
            return None, "expected a JSON object"
        if spec.key == "tools.alias_profiles":
            error = _validate_alias_profiles(value)
            if error is not None:
                return None, error
        if spec.key == "limits.shell_env_allow":
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                return None, "expected a JSON list of non-empty environment-variable names"
        if spec.key in {"mcp.servers", "mcp.agents"}:
            from . import mcp_config

            try:
                if spec.key == "mcp.servers":
                    mcp_config.parse_servers(value)
                else:
                    mcp_config.parse_agents(value)
            except mcp_config.MCPConfigError as exc:
                return None, str(exc)
        return value, None
    return text, None  # str


def _validate_alias_profiles(value: Any) -> str | None:
    if not isinstance(value, list):
        return "expected a JSON list"
    for profile in value:
        if not isinstance(profile, dict):
            return "expected profiles with match and aliases"
        match = profile.get("match")
        aliases = profile.get("aliases")
        if not isinstance(match, (str, list)) or not isinstance(aliases, dict):
            return "expected profiles with match and aliases"
        if not aliases:
            return "expected non-empty aliases"
        matches = [match] if isinstance(match, str) else match
        if not matches or any(not isinstance(item, str) or not item.strip() for item in matches):
            return "expected non-empty match values"
        seen_aliases: set[str] = set()
        for canonical, alias in aliases.items():
            if not isinstance(canonical, str) or _TOOL_ALIAS_NAME_RE.fullmatch(canonical) is None:
                return "expected canonical tool names matching [A-Za-z0-9_-]+"
            if not isinstance(alias, str) or _TOOL_ALIAS_NAME_RE.fullmatch(alias) is None:
                return "expected alias names matching [A-Za-z0-9_-]+"
            key = alias.lower()
            if key in seen_aliases:
                return "expected unique alias names"
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


def _prefix_spec(key: str) -> SettingSpec | None:
    for spec in REGISTRY:
        if key.startswith(spec.key + "."):
            return spec
    return None


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
    spec = SPEC_BY_KEY.get(key)
    if spec is not None:
        value, error = coerce_value(spec, raw_value)
        if error is not None:
            raise ValueError(f"--extra {key}: {error}")
        return spec.path, value
    prefix_spec = _prefix_spec(key)
    if prefix_spec is not None and prefix_spec.type != "map":
        raise ValueError(f"--extra unknown knob: {key}")
    return _parse_dotted_key(key), coerce_extra_value(raw_value)


def apply_cli_extras(settings: dict, extras: list[str]) -> dict:
    for arg in extras:
        path, value = parse_extra_arg(arg)
        set_dotted(settings, path, value)
    return settings


# ---------------------------------------------------------------------------
# Env layer
# ---------------------------------------------------------------------------

def canonical_env_name(key: str) -> str:
    """The deterministic env var for any dotted knob: ``a.b.c`` -> ``JS_A_B_C``.

    Every knob is reachable from the environment under this name, so env and
    jsrc stay at parity (``JS_SAMPLING_TOP_P`` <-> ``set sampling.top_p``). A
    spec may ALSO carry a shorter hand-picked ``env`` alias (``JS_MODEL`` for
    ``model.id``); that alias wins when both are set."""
    return "JS_" + key.upper().replace(".", "_")


def env_names_for(spec: SettingSpec) -> tuple[str, ...]:
    """Env vars that feed ``spec``: the hand-picked alias first (highest
    precedence), then the canonical ``JS_<DOTTED>`` form."""
    canon = canonical_env_name(spec.key)
    if spec.env and spec.env != canon:
        return (spec.env, canon)
    return (canon,)


def apply_env_overrides(settings: dict, env: dict[str, str] | None = None) -> dict:
    """Overlay JS_* env vars onto ``settings``. Each knob accepts its canonical
    ``JS_<DOTTED>`` name plus any hand-picked alias; the alias wins."""
    source = env if env is not None else os.environ
    for spec in REGISTRY:
        for name in env_names_for(spec):
            if name not in source:
                continue
            value, error = coerce_value(spec, source[name])
            if error is not None:
                # garbage in the env: skip rather than clobber a working value,
                # but say so — a silently dropped JS_BASE_URL costs an evening
                print(f"js: ignoring {name}: {error}", file=sys.stderr)
                continue
            set_dotted(settings, spec.path, value)
            break
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
    "kernel": [
        "# How the persistent-kernel tools render to YOUR terminal.",
        "# The model always receives the full result; these only change what you see.",
    ],
    "runtime": ["# Live-runtime toggles."],
    "compact": ["# Cache-first context compaction knobs."],
    "subagents": ["# Subagent model-selection policy."],
    "tools": ["# Model-facing tool aliasing."],
    "mcp": [
        "# MCP server connection definitions and per-agent allow/deny policy.",
        "# Server values may contain credentials; /set and /show mask mcp.servers.",
    ],
    "sampling": ["# Per-turn sampling overrides. Default display is <unset>; provider/model defaults win."],
}


def _template_value(spec: SettingSpec) -> str:
    default = spec.default
    if default is None:
        return ""
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
    lines.append("# Every knob also reads a canonical JS_<DOTTED_UPPER> env var:")
    lines.append("#   set sampling.top_p  <->  JS_SAMPLING_TOP_P")
    lines.append("# Some knobs carry a shorter hand-picked alias too (it wins when both are set):")
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


# ---------------------------------------------------------------------------
# /save — snapshot the live settings back into a jsrc set-script
# ---------------------------------------------------------------------------

_MISSING = object()


def _config_line_value(spec: SettingSpec, value: Any) -> str:
    """Render ``value`` as the right-hand side of a `set <key> <value>` line —
    the inverse of `coerce_value`, so a saved line reloads to the same value."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _equals_default(spec: SettingSpec, value: Any) -> bool:
    """True when ``value`` is the knob's built-in default (nothing to persist)."""
    default = spec.default
    if value is None or value == "":
        return default is None
    return value == default


def settings_diff_lines(settings: dict) -> list[str]:
    """`set <key> <value>` lines for every knob whose current value differs from
    its built-in default, in REGISTRY order. Secrets are written verbatim — the
    jsrc key lines are plain (see the template's `#set provider.api_key`)."""
    lines: list[str] = []
    for spec in REGISTRY:
        value = get_dotted(settings, spec.path, _MISSING)
        if value is _MISSING:
            value = None
        if _equals_default(spec, value):
            continue
        lines.append(f"set {spec.key} {_config_line_value(spec, value)}")
    return lines


def save_settings_to_jsrc(
    path: Path,
    settings: dict,
    *,
    stamp: str | None = None,
    source: str = "/save",
) -> tuple[int, Path | None]:
    """Write the non-default knobs in ``settings`` to ``path`` as a jsrc script.

    An existing file is copied to ``<name>.bak`` beside itself first. Returns
    ``(knob_count, backup_path_or_None)``."""
    lines = settings_diff_lines(settings)
    backup: Path | None = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    if stamp is None:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = [
        f"# js config — written by {source} on {stamp}.",
        "# Each non-comment line is a `set <key> <value>` command; only knobs that",
        "# differ from built-in defaults are listed.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*header, *lines]) + "\n", encoding="utf-8")
    return len(lines), backup
