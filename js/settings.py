"""Knob registry and config loader for the js harness.

`REGISTRY` (a list of `SettingSpec`) names every runtime knob: its storage
path, type, env-var override, empty-state display, and help text. The values
a knob starts with live in ONE file, `js/jsrc`, shipped in the package: one
`set` line per knob. `seed_defaults` and `default_value` read it; a missing
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
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import reasoning as _reasoning
from . import messages as msgs

# What a line typed while a turn runs does: now = join the running turn at its
# next tool boundary, batch = one message after the turn, one = one turn per line.
STEER_MODES: tuple[str, ...] = ("now", "batch", "one")

# The built-in default layer: one `set` line per registered knob.
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
    doc: str
    env: str | None = None      # JS_* env var feeding the env layer, if any
    empty: str = EMPTY_NONE     # how an unset value renders
    live: bool = True           # settable live in the REPL
    secret: bool = False        # mask the value in `show`
    aliases: tuple[str, ...] = ()  # short names `set`/`show` accept for ``key``

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.key.split("."))

    @property
    def default(self) -> Any:
        """This knob's value in `js/jsrc`; None when that file leaves it unset."""
        return default_value(self.key)

    @property
    def section(self) -> str:
        return self.path[0]


# Every knob. Order here is the order `show` uses.
REGISTRY: tuple[SettingSpec, ...] = (
    # --- agent ---
    SettingSpec("agent", "str",
                "Agent a run uses when neither --agent nor --commit names one: a prompt "
                "directory id, letters, digits, '_' or '-'. Read once at startup, so a "
                "`set` here reaches later runs through /save."),
    # --- model ---
    SettingSpec("model.id", "str",
                "Default model id; unprefixed ids route through AI Gateway.",
                env="JS_MODEL", aliases=("model",)),
    SettingSpec("model.max_output_tokens", "int",
                "Per-call max_tokens; unset = models.dev metadata when known, else no explicit cap.",
                env="JS_MAX_OUTPUT_TOKENS", empty=EMPTY_NONE),
    SettingSpec("model.context_window", "int",
                "Override the active model's context window. Unset = local runtime "
                "allocation, then models.dev metadata. Beats every other source; for a "
                "multi-model setup use compact.context_window_overrides instead.",
                env="JS_CONTEXT_WINDOW", empty=EMPTY_NONE),
    SettingSpec("model.reasoning_effort", "str",
                "Thinking effort: off|minimal|low|medium|high|xhigh|max. off disables thinking. "
                "Any other value is rejected. Clear with `set -model.reasoning_effort`.",
                env="JS_REASONING", empty=EMPTY_NONE),
    SettingSpec("model.thinking_budget", "int",
                "Thinking budget in tokens on the Anthropic wire, for the models that take a "
                "budget: Claude 4.5 and earlier, MiniMax, opencode-go. Unset = derived from "
                "model.reasoning_effort. Adaptive models and effort off ignore it.",
                empty=EMPTY_NONE),
    SettingSpec("model.vision", "bool",
                "Send image bytes to the active model: on/off; unset = detect from "
                "models.dev input modalities, then curated name hints. Clear with "
                "`set -model.vision`.",
                env="JS_VISION", empty=EMPTY_NONE),
    # --- ui ---
    SettingSpec("ui.reasoning", "int",
                "Reasoning display: 0 hidden, 1 stream then collapse, 2 leave visible, "
                "3 leave visible with token counts. The reasoning_toggle key, Ctrl-O "
                "unless keys.file moves it, folds and unfolds it in the async screen. "
                "Display only; session reasoning is always retained."),
    SettingSpec("ui.net", "int",
                "Network display in the async screen: 0 nothing, 1 failures, 2 also "
                "Connecting/Connected lines and a response byte counter on the status "
                "bar until the first token, 3 also retries, catalog refreshes and "
                "per-call stream stats."),
    SettingSpec("ui.status_bg", "str",
                "Status bar background, #rrggbb. Drawn in truecolor on every terminal."),
    SettingSpec("ui.status_fg", "str",
                "Status bar foreground, #rrggbb."),
    SettingSpec("ui.tools", "int",
                "Tool exchange display: 0 nothing, 1 one metrics line per exchange, "
                "2 the call plus the first ui.tools_preview_lines result lines and "
                "shown/total metrics, 3 the call plus the whole result."),
    SettingSpec("ui.tools_preview_lines", "int",
                "Lines of a tool's command and of its result shown at ui.tools 2."),
    SettingSpec("ui.markdown", "bool",
                "Render assistant Markdown on a terminal: finished blocks are "
                "highlighted once, the open block stays live. Off writes the text "
                "as it arrives. Output that is not a terminal is always plain text."),
    SettingSpec("ui.resume_exchanges", "int",
                "Exchanges of a resumed session the REPL shows before its prompt, "
                "drawn as a turn draws them. 0 shows none."),
    SettingSpec("ui.editing_mode", "str",
                "Input line key bindings in the async screen. emacs: Enter sends. vi: a "
                "multi-line buffer, where Esc then `:` opens the ex line and `:x` sends."),
    SettingSpec("ui.paste_image_key", "str",
                "Key that pastes the clipboard image into the input line as [image #N]; "
                "the image is sent with the line like an @path image. prompt_toolkit "
                "key names, space-separated: c-v, escape v. Unset binds no key."),
    SettingSpec("ui.paste_collapse_lines", "int",
                "A bracketed paste of more lines than this shows in the input line "
                "as one [paste #N +X lines] marker; the line sends the full text. "
                "0 = no line limit."),
    SettingSpec("ui.paste_collapse_chars", "int",
                "A bracketed paste of more characters than this shows as one "
                "[paste #N X chars] marker; the line sends the full text. 0 = no "
                "character limit. Both 0 never collapses a paste."),
    SettingSpec("ui.paste_image_command", "str",
                "Command that prints the clipboard image. Unset = `wl-paste --type "
                "image/png` under Wayland, `xclip -selection clipboard -t image/png -o` "
                "under X11.",
                empty=EMPTY_NONE),
    # --- history ---
    SettingSpec("history.file", "str",
                "Prompt history: every line sent at a REPL prompt, one JSON object per "
                "line with ts, cwd, session, agent and text, shared by every js run. "
                "Unset = ~/.js/state/history.jsonl. Read when the REPL starts.",
                empty=EMPTY_NONE),
    SettingSpec("history.cwd_first", "bool",
                "Up offers the prompts typed in the current directory first, newest "
                "first, then the rest; off, every prompt in time order. The "
                "history_search key, Ctrl-R, searches all of them either way. Read "
                "when the REPL starts.", empty=EMPTY_OFF),
    SettingSpec("history.max_entries", "int",
                "Newest prompts loaded from history.file for Up and Ctrl-R. Read when "
                "the REPL starts."),
    # --- keys ---
    SettingSpec("keys.file", "str",
                "Key bindings file: `bind KEY... ACTION` and `unbind KEY...` lines over "
                "the defaults; a bad line is one error at startup and is skipped. "
                "Unset = ~/.js/keys. Actions: submit, history_search, ex_open, ex_run, "
                "ex_cancel, interrupt, eof, suspend, reasoning_toggle, redraw, "
                "scroll_up, scroll_down, complete. Read when the REPL starts.",
                empty=EMPTY_NONE),
    # --- tags ---
    SettingSpec("tags.file", "str",
                "Session tag list: one `name: description` line per tag. Unset = "
                "~/.js/tags.yaml when it exists, else the stock list js ships (js/tags.yaml). "
                "A named file that is missing, or no TYPESAFE_API_KEY, means no tagging. "
                "Editing the list retags every shown session at the next session end.",
                empty=EMPTY_NONE),
    SettingSpec("tags.threshold", "float",
                "Lowest Jev score, 0 to 1, a tag needs to be kept on a session."),
    SettingSpec("tags.max", "int",
                "Most tags a session keeps, highest score first."),
    SettingSpec("tags.messages", "int",
                "How many of a session's last operator and model messages Jev reads "
                "to tag it. Tool output is never sent."),
    SettingSpec("tags.message_chars", "int",
                "Longest text sent per message when tagging; longer messages are cut."),
    SettingSpec("tags.model", "str",
                "TypeSafe model that judges session tags."),
    # --- provider ---
    SettingSpec("provider.id", "str",
                "Explicit js provider id, e.g. deepseek, openai-codex, ollama.",
                env="JS_PROVIDER", empty=EMPTY_NONE, aliases=("provider",)),
    SettingSpec("provider.base_url", "str",
                "Explicit provider base URL; unset = provider default.",
                env="JS_BASE_URL", empty=EMPTY_NONE, aliases=("baseurl",)),
    SettingSpec("provider.api_key", "str",
                "Explicit provider API key; unset = env/login default.",
                env="JS_API_KEY", empty=EMPTY_NONE, secret=True, aliases=("apikey",)),
    SettingSpec("provider.extra", "map",
                "Free-form extra params passed through to the provider SDK.",
                empty=EMPTY_NONE),
    # --- limits ---
    SettingSpec("limits.max_tool_iterations", "int",
                "Max tool calls per turn before the loop gives up.",
                env="JS_MAX_TOOL_ITERATIONS"),
    SettingSpec("limits.max_tool_calls_per_message", "int",
                "Maximum distinct tool calls accepted from one assistant message; "
                "duplicates are collapsed before this ceiling is applied.",
                env="JS_MAX_TOOL_CALLS_PER_MESSAGE"),
    SettingSpec("limits.max_bash_output_bytes", "int",
                "Shell output shown per call, stdout and stderr together. Past it, the "
                "head and tail are shown and the whole stream is written to a file.",
                env="JS_MAX_BASH_OUTPUT_BYTES"),
    SettingSpec("limits.max_bash_output_ceiling", "int",
                "Upper bound a caller may raise max_bash_output_bytes to; the effective "
                "shell cap is min(max_bash_output_bytes, this)."),
    SettingSpec("limits.max_tool_result_inline_bytes", "int",
                "Results larger than this are written to a file and replaced with a "
                "preview plus the path, instead of being clipped and lost. 0 = off."),
    SettingSpec("limits.max_tool_result_bytes", "int",
                "Hard cap on any tool result string.",
                env="JS_MAX_TOOL_RESULT_BYTES"),
    SettingSpec("limits.fetch_timeout_s", "int",
                "fetch() whole-request deadline in seconds, and the per-call timeout for the "
                "web-search backends' JSON calls.",
                env="JS_FETCH_TIMEOUT"),
    SettingSpec("limits.shell_env_allow", "json",
                "Environment-variable names inherited by shell() without naming "
                "them per call in env; a JSON string list."),
    SettingSpec("limits.browse_timeout_s", "int",
                "browse() page budget in seconds. obscura is told to give up one "
                "second earlier so its own graceful navigation-timeout path runs "
                "and partial content survives.",
                env="JS_BROWSE_TIMEOUT"),
    SettingSpec("limits.download_timeout_s", "int",
                "aria2c transfer timeout in seconds for saved, binary, and oversized "
                "fetch() responses. Downloads are bounded by size, not by how fast a "
                "page renders.",
                env="JS_DOWNLOAD_TIMEOUT"),
    SettingSpec("limits.max_download_bytes", "int",
                "Size ceiling for fetch(save=...) in bytes. 0 = unlimited: a save "
                "streams to disk and never lands in memory, so an ISO or a model "
                "weight is a normal download. Set a number only to impose a quota.",
                env="JS_MAX_DOWNLOAD_BYTES"),
    SettingSpec("limits.inline_code_timeout_s", "int",
                "Timeout in seconds for !{sh|python|c|node ...} and ```!lang prompt expansions.",
                env="JS_INLINE_CODE_TIMEOUT"),
    SettingSpec("limits.max_read_lines", "int",
                "Maximum lines returned by read()."),
    SettingSpec("limits.max_file_bytes", "int",
                "Maximum file bytes read by fs tools."),
    SettingSpec("limits.max_text_attachment_bytes", "int",
                "Most bytes of a text file attached to a prompt; a larger file is "
                "truncated. limits.max_tool_result_bytes also caps it."),
    SettingSpec("limits.max_read_bytes", "int",
                "Maximum file bytes for a whole-file read(); ignored when the call "
                "passes a line or byte range, so ranged reads work on any size file."),
    SettingSpec("limits.max_tool_results_per_turn_bytes", "int",
                "Aggregate cap on all tool results returned by one batch of parallel "
                "calls; the largest results are clipped first. 0 = unlimited."),
    SettingSpec("limits.task_max_depth", "int",
                "Maximum recursive task/subagent depth."),
    SettingSpec("limits.subagent_max_workers", "int",
                "Maximum concurrent subagent workers per task call; minimum 1."),
    # --- kernel ---
    SettingSpec("kernel.verbosity", "str",
                "How much of each kernel/toolbox call is rendered to your terminal. "
                "quiet: errors and interrupts only. normal: code, output, timing, "
                "namespace. verbose: stdout, stderr and display split out, plus kernel "
                "lifecycle and toolbox activity. Affects only what you see; the model "
                "always receives the full result.",
                env="JS_KERNEL_VERBOSITY"),
    SettingSpec("kernel.render_max_lines", "int",
                "Line cap per section of that terminal render, so a 4000-line cell "
                "cannot scroll the screen away. The hidden count is always shown, and "
                "the model still gets the untrimmed output."),
    SettingSpec("shell.program", "str",
                "Program the `shell` tool runs commands with: a name looked up on "
                "PATH, or a path. bash and zsh run with `-o pipefail`, so a pipeline "
                "fails when any stage fails."),
    SettingSpec("shell.wait_seconds", "int",
                "Seconds a `shell` call waits for its command before returning a "
                "handle to poll. The command keeps running; nothing is killed by "
                "this wait."),
    SettingSpec("kernel.wait_seconds", "int",
                "Seconds a `kernel` call waits for a submitted cell before returning "
                "a handle to poll. The cell keeps running; nothing is interrupted by "
                "this wait."),
    # --- jail ---
    SettingSpec("jail.bind", "json",
                "Under `js -C DIR`, more paths the jail shows: a JSON list of "
                "entries, absolute or starting with ~. \"path\" is read-only and "
                "\"path:rw\" read-write. A path that does not exist is skipped. Commands see "
                "each at its real path; the file tools may read it, and write it when "
                "it is :rw."),
    # --- runtime ---
    SettingSpec("runtime.debug", "bool",
                "Append per-event records to state/<agent>/debug.log.",
                env="JS_DEBUG", empty=EMPTY_OFF),
    SettingSpec("runtime.trace", "bool",
                "Pretty-print the tool-call trace line as the model runs.",
                env="JS_TRACE", empty=EMPTY_OFF),
    SettingSpec("runtime.max_parallel_tools", "int",
                "Most read-only tool calls of one model response that run at once: "
                "read, fs_search, the web searches, a GET fetch. A call that writes "
                "runs alone, after the calls before it. 1 runs every call in turn."),
    SettingSpec("runtime.retry_attempts", "int",
                "Retries of one model request after a retryable failure: a 429, a "
                "5xx, a dropped or idle stream. Each waits what the provider's "
                "Retry-After or retry-after-ms header asks, else 1s, 2s, 4s ... up to "
                "16s. 0 fails on the first error."),
    SettingSpec("runtime.retry_max_wait_seconds", "float",
                "Longest Retry-After wait honoured. A provider asking for longer "
                "fails the request at once. 0 honours any wait."),
    SettingSpec("runtime.stream_idle_seconds", "float",
                "Seconds a model request may go without a response byte before it "
                "is aborted and retried under runtime.retry_attempts. SSE keep-alive "
                "comments count as bytes. 0 waits forever."),
    SettingSpec("runtime.max_output_escalation", "int",
                "A reply cut off by its output-token cap is sent again once with "
                "this cap, when it is larger than the one used and not above the "
                "model's known output limit. 0 never resends."),
    SettingSpec("runtime.max_output_resumes", "int",
                "After a reply is cut off by its output-token cap, how many times per "
                "turn the partial reply is kept and the model is told to resume. 0 "
                "ends the turn on the cutoff."),
    SettingSpec("runtime.steer", "str",
                "What a line typed while a turn runs does. now: it reaches the model "
                "at the turn's next tool boundary, as a user message. A turn with no "
                "boundary left gets it after it ends, as with batch. batch: every "
                "line typed during the turn goes in as ONE message after it ends. "
                "one: each line is its own turn, in order."),
    SettingSpec("runtime.debug_autolog", "bool",
                "Append the full request trace to ~/.js/logs/<agent>/<session>.log: the "
                "unclipped system prompt, tool-schema JSON, and the messages sent each call. "
                "This trace never prints to the terminal, only to the file.",
                env="JS_DEBUG_AUTOLOG", empty=EMPTY_OFF),
    SettingSpec("runtime.debug_autolog_dir", "str",
                "Directory for the debug autolog; unset = ~/.js/logs/<agent>.",
                env="JS_DEBUG_AUTOLOG_DIR", empty=EMPTY_NONE),
    SettingSpec("runtime.transcript_log", "bool",
                "Append the visible terminal/TUI transcript to "
                "~/.js/logs/transcript/<agent>/<session>.log: what printed to the user, with "
                "IRC-style <USER>/<APE> tags for user/assistant turns.",
                env="JS_TRANSCRIPT_LOG", empty=EMPTY_OFF),
    SettingSpec("runtime.transcript_log_dir", "str",
                "Directory for the visible transcript log; unset = ~/.js/logs/transcript/<agent>.",
                env="JS_TRANSCRIPT_LOG_DIR", empty=EMPTY_NONE),
    SettingSpec("runtime.allow_inline_code", "bool",
                "Execute !{sh|python|c|node ...} inline directives / ```!lang fences in "
                "prompt files and inject their stdout. This runs arbitrary code from "
                "prompt files; opt out with --im-a-pussy or set this off.",
                env="JS_ALLOW_INLINE_CODE", empty=EMPTY_OFF),
    # --- events ---
    SettingSpec("events.exec_timeout_s", "int",
                "Seconds an `exec` command, typed or run by an `on` handler, may run "
                "before its process group is killed; 0 is no limit. A handler's "
                "command holds up the event that ran it for that long."),
    SettingSpec("events.exec_output_bytes", "int",
                "Bytes of an `exec` command's stdout and stderr kept; the stdout "
                "reaches the model as a js-reminder on the next message. 0 keeps all."),
    # --- compact ---
    SettingSpec("compact.auto", "bool",
                "Automatic cache-aware context compaction.", empty=EMPTY_OFF),
    SettingSpec("compact.flight_log_dir", "str",
                "Full compaction flight snapshots; unset = logs/<agent>/compactions.", empty=EMPTY_NONE),
    SettingSpec("compact.context_window", "int",
                "Context window tokens for fullness math; unset = models.dev metadata.",
                empty=EMPTY_NONE),
    SettingSpec("compact.context_window_overrides", "map",
                "Per-model context windows, keyed 'provider/model' or 'model'. "
                "'provider/model' is the more specific. For surfaces models.dev has no row for — a subscription "
                "endpoint serving the same model id as the public API with a different "
                "usable window.", empty=EMPTY_NONE),
    SettingSpec("compact.context_window_fallback", "int",
                "Window to assume ONLY for models whose size cannot be resolved. Unlike "
                "context_window this does not override models that are known, so covering "
                "one unknown model no longer shrinks every known one.",
                empty=EMPTY_NONE),
    SettingSpec("compact.notify_threshold", "float",
                "Notify once when context reaches this fraction."),
    SettingSpec("compact.trigger_threshold", "float",
                "Auto-compact at this fullness fraction."),
    SettingSpec("compact.force_threshold", "float",
                "Force compact at this fullness fraction."),
    SettingSpec("compact.buffer_tokens", "int",
                "Extra input-token headroom reserved by preflight/mid-turn compaction."),
    SettingSpec("compact.summary_reserve_tokens", "int",
                "Ceiling on the reply headroom subtracted before the fullness fractions; "
                "the actual reserve is min(model max_output_tokens, this). Stops a model "
                "declaring a 128k output cap from eating a third of the window."),
    SettingSpec("compact.tail_tokens", "int",
                "Recent tail budget retained after compaction."),
    SettingSpec("compact.min_savings_tokens", "int",
                "Skip compaction unless estimated savings exceeds this."),
    SettingSpec("compact.clear_keep_recent", "int",
                "Tool results left intact when an over-budget request clears old "
                "tool-result bodies before falling back to a summary."),
    SettingSpec("compact.rehydrate_max_files", "int",
                "Recently read files re-attached after a summary compaction, "
                "newest first. 0 = none."),
    SettingSpec("compact.rehydrate_token_budget", "int",
                "Estimated tokens all re-attached files may use together."),
    SettingSpec("compact.rehydrate_max_tokens_per_file", "int",
                "A recently read file larger than this many estimated tokens is "
                "named after a compaction but not re-attached."),
    SettingSpec("compact.chars_per_token", "float",
                "Fallback/self-calibrating character-to-token estimate."),
    SettingSpec("compact.model", "str",
                "Model used to write the compaction summary; 'same' = active model."),
    SettingSpec("compact.summary_max_tokens", "int",
                "Max tokens for the compaction summary. Capped at 8192."),
    SettingSpec("compact.summary_tool_result_chars", "int",
                "Longest tool result, in characters, that a summary request carries "
                "whole; a longer one keeps its head and tail. 0 = whole results."),
    SettingSpec("compact.max_summary_failures", "int",
                "Failed automatic summaries in a row that pause automatic compaction. "
                "A successful /compact resumes it."),
    SettingSpec("compact.cache_ttl_seconds", "int",
                "Prompt-cache lifetime. An over-budget request clears old tool results "
                "first when the last request is at least this old; before that it "
                "summarizes earlier turns first. 0 = always clear first."),
    SettingSpec("compact.pre_hook", "str",
                "Optional shell command whose stdout guides compaction.",
                empty=EMPTY_NONE),
    # --- subagents ---
    SettingSpec("subagents.prefer_inherit", "bool",
                "Subagents inherit the parent's model when true; else use the agent's own primary.",
                empty=EMPTY_OFF),
    SettingSpec("subagents.lock_model", "bool",
                "When true, the main agent cannot pick a subagent model via the task tool.",
                empty=EMPTY_OFF),
    # --- tools ---
    SettingSpec("tools.alias_profiles", "json",
                "Model-facing tool-name alias profiles: list of {match:string|[...], aliases:{...}}.",
                empty=EMPTY_NONE),
    SettingSpec("tools.user_agent", "str",
                "User-Agent header fetch and the web-search backends send when a call "
                "names none."),
    SettingSpec("tools.terminal_cols", "int",
                "Columns of a terminal_session started without cols."),
    SettingSpec("tools.terminal_rows", "int",
                "Rows of a terminal_session started without rows."),
    # --- lsp ---
    SettingSpec("lsp.servers", "json",
                "Language servers the `lsp` tool may start: a JSON list of "
                "{name, command, extensions, roots}. For a file, the first entry "
                "whose extensions hold its extension and whose command[0] is on "
                "PATH is used. roots are the files that mark a workspace root; "
                "without one, the nearest directory holding .git is the root."),
    SettingSpec("lsp.timeout_s", "int",
                "Seconds an `lsp` call waits for its language server: to start, "
                "to answer a request, or to publish diagnostics."),
    # --- notebook ---
    SettingSpec("notebook.output_lines", "int",
                "Lines of each cell output a `read` of an .ipynb shows; the rest "
                "is counted. 0 shows only what kind each output is."),
    # --- mcp ---
    SettingSpec("mcp.servers", "json",
                "Named MCP servers as JSON: stdio uses command/args/env; streamable HTTP uses url/headers.",
                empty=EMPTY_NONE, secret=True),
    SettingSpec("mcp.agents", "json",
                "Per-agent MCP policy JSON with servers/tools allow and deny glob lists.",
                empty=EMPTY_NONE),
    SettingSpec("mcp.request_timeout_s", "float",
                "Seconds an MCP request waits for its server's reply: initialize, "
                "list, call or read."),
    # --- sampling ---
    SettingSpec("sampling.temperature", "float",
                "Provider-default sampling temperature; unset = do not send.",
                empty=EMPTY_UNSET),
    SettingSpec("sampling.top_p", "float",
                "Provider-default nucleus sampling top_p; unset = do not send.",
                empty=EMPTY_UNSET),
    SettingSpec("sampling.top_k", "int",
                "Provider-default top_k sampling; unset = do not send.",
                empty=EMPTY_UNSET),
    SettingSpec("sampling.repetition_penalty", "float",
                "Provider-default repetition penalty; unset = do not send.",
                empty=EMPTY_UNSET),
    SettingSpec("sampling.presence_penalty", "float",
                "Provider-default presence penalty; unset = do not send.",
                empty=EMPTY_UNSET),
)

SPEC_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in REGISTRY}
SPEC_BY_ALIAS: dict[str, SettingSpec] = {alias: spec for spec in REGISTRY for alias in spec.aliases}


def spec_for(name: str) -> SettingSpec | None:
    """The spec a `set`/`show` name refers to: its dotted key or a short alias."""
    return SPEC_BY_KEY.get(name) or SPEC_BY_ALIAS.get(name)
KNOWN_SECTIONS: frozenset[str] = frozenset(spec.section for spec in REGISTRY)


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
    e.g. `c-v` or `escape v`. The form `ui.paste_image_key` takes."""
    from prompt_toolkit.key_binding import KeyBindings

    keys = value.split() if isinstance(value, str) else []
    if not keys:
        return False
    try:
        KeyBindings().add(*keys)
    except ValueError:
        return False
    return True

# The only values `model.reasoning_effort` accepts: the effort ladder in
# js/reasoning.py, with its bottom stop "none" spelled "off" (stored as the
# literal "none"). Everything else is rejected outright — no
# default/auto/unset synonyms. Clearing the knob back to provider-default is
# `set -model.reasoning_effort`, never a magic value here.
REASONING_EFFORT_VALUES: tuple[str, ...] = ("off", *_reasoning.EFFORT_LADDER[1:])
_REASONING_EFFORT_ERROR = "expected " + "|".join(REASONING_EFFORT_VALUES)


def steer_mode(value: Any) -> str:
    """The runtime.steer mode ``value`` names, or its js/jsrc value for anything else."""
    text = str(value or "").strip().lower()
    return text if text in STEER_MODES else default_value("runtime.steer")


def parse_bool(raw: str) -> bool | None:
    v = raw.strip().lower()
    if v in _TRUE_TOKENS:
        return True
    if v in _FALSE_TOKENS:
        return False
    return None


_NONNEGATIVE_KNOBS = frozenset({
    "runtime.retry_attempts", "runtime.retry_max_wait_seconds", "runtime.stream_idle_seconds",
    "runtime.max_output_escalation", "runtime.max_output_resumes",
})


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
    if spec.key == "runtime.steer":
        v = text.lower()
        if v not in STEER_MODES:
            return None, msgs.EXPECTED_ONE_OF.text(choices="|".join(STEER_MODES))
        return v, None
    if spec.key == "provider.id" and text:
        from . import providers as _providers

        if _providers.get_provider(text) is None:
            return None, msgs.UNKNOWN_PROVIDER_ID.text(provider=text)
        return text, None
    if spec.key in {"ui.status_bg", "ui.status_fg"} and not is_hex_colour(text):
        return None, msgs.EXPECTED_COLOUR.text(value=text)
    if spec.key == "ui.editing_mode":
        if text not in ("emacs", "vi"):
            return None, msgs.EXPECTED_ONE_OF.text(choices="emacs|vi")
        return text, None
    if spec.key == "ui.paste_image_key":
        if not is_key_name(text):
            return None, msgs.EXPECTED_KEY.text(value=text)
        return " ".join(text.split()), None
    if spec.key == "provider.base_url" and text:
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
            "limits.max_tool_calls_per_message", "limits.subagent_max_workers", "ui.tools_preview_lines",
            "tools.terminal_cols", "tools.terminal_rows", "runtime.max_parallel_tools",
            "history.max_entries", "lsp.timeout_s", "tags.max", "tags.messages", "tags.message_chars",
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
        if spec.key == "mcp.request_timeout_s" and number <= 0:
            return None, msgs.EXPECTED_POSITIVE_NUMBER.text()
        if spec.key == "tags.threshold" and not 0 <= number <= 1:
            return None, msgs.EXPECTED_PROBABILITY.text()
        return number, None
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
                return None, msgs.EXPECTED_JSON.text()
        if kind == "map" and not isinstance(value, dict):
            return None, msgs.EXPECTED_JSON_OBJECT.text()
        if spec.key == "tools.alias_profiles":
            error = _validate_alias_profiles(value)
            if error is not None:
                return None, error
        if spec.key == "limits.shell_env_allow":
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                return None, msgs.EXPECTED_ENV_NAMES.text()
        if spec.key == "jail.bind":
            from . import jail

            try:
                jail.parse_binds(value)
            except ValueError as exc:
                return None, str(exc)
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
    """The registered knob ``key`` sits under (`provider.extra` for
    `provider.extra.organization`), or None."""
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
        raise ValueError(msgs.EXTRA_NOT_KEY_VALUE.text(arg=arg))
    raw_key, raw_value = arg.split("=", 1)
    key = raw_key.strip()
    if not key:
        raise ValueError(msgs.EXTRA_EMPTY_KEY.text(arg=arg))
    if raw_value == "":
        raise ValueError(msgs.EXTRA_EMPTY_VALUE.text(arg=arg))
    spec = SPEC_BY_KEY.get(key)
    if spec is not None:
        value, error = coerce_value(spec, raw_value)
        if error is not None:
            raise ValueError(msgs.EXTRA_BAD_VALUE.text(key=key, error=error))
        return spec.path, value
    prefix_spec = parent_spec(key)
    if prefix_spec is not None and prefix_spec.type != "map":
        raise ValueError(msgs.EXTRA_UNKNOWN_SETTING.text(key=key))
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
    registered knob has no line in it."""
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
    """Registered knob ``key``'s value in `js/jsrc`; None when that file leaves
    it unset."""
    spec = spec_for(key)
    path = spec.path if spec is not None else tuple(key.split("."))
    return copy.deepcopy(get_dotted(_package_settings(), path))


def knob(settings: dict | None, key: str) -> Any:
    """Knob ``key`` from a settings store, or its `js/jsrc` value when the
    store does not hold it."""
    spec = spec_for(key)
    path = spec.path if spec is not None else tuple(key.split("."))
    missing = object()
    value = get_dotted(settings or {}, path, missing)
    return default_value(key) if value is missing else value


def knob_attr(obj: Any, attr: str, key: str) -> Any:
    """``obj.attr`` (a Config or ToolContext field), or knob ``key``'s js/jsrc
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
# /save — snapshot the live settings back into a jsrc set-script
# ---------------------------------------------------------------------------

def _config_line_value(spec: SettingSpec, value: Any) -> str:
    """Render ``value`` as the right-hand side of a `set <key> <value>` line —
    the inverse of `coerce_value`, so a saved line reloads to the same value."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def settings_lines(settings: dict) -> list[str]:
    """One line per registered setting, in REGISTRY order: `set <key> <value>`
    for its value in ``settings``, `set -<key>` when it holds none. Secrets are
    written verbatim."""
    lines: list[str] = []
    for spec in REGISTRY:
        value = get_dotted(settings, spec.path)
        if value is None or value == "":
            lines.append(f"set -{spec.key}")
        else:
            lines.append(f"set {spec.key} {_config_line_value(spec, value)}")
    return lines


def save_settings_to_jsrc(
    path: Path,
    settings: dict,
    *,
    extra_lines: list[str] | None = None,
    stamp: str | None = None,
    source: str = "/save",
) -> tuple[int, Path | None]:
    """Replace ``path`` with a jsrc script holding every registered setting's
    value in ``settings``, followed by ``extra_lines`` (other commands to
    replay, e.g. `on` and `alias` lines).

    An existing file is copied to ``<name>.bak`` beside itself first. Returns
    ``(line_count, backup_path_or_None)``."""
    lines = [*settings_lines(settings), *(extra_lines or [])]
    backup: Path | None = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    if stamp is None:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = [
        f"# js config — written by {source} on {stamp}.",
        "# Each non-comment line is a command: one `set` line per setting, then",
        "# handlers and aliases. `set -<key>` leaves a setting unset.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*header, *lines]) + "\n", encoding="utf-8")
    return len(lines), backup


def ensure_user_jsrc(path: Path) -> bool:
    """Write ``path`` with every setting at its `js/jsrc` value when there is no
    file there. Returns whether it wrote one."""
    if path.exists():
        return False
    save_settings_to_jsrc(path, seed_defaults(), source="js on first run")
    return True
