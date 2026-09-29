"""Every string js shows the operator, by name.

Code names an entry here and passes the values for its holes; the wording
lives only in this file. Tool results, prompts and tool descriptions, which the
model reads, are not here. The commit helper's entries are: the operator runs
it, and the commit agent reads the same output through a shell.

A `Message` is in one register:

  banner   the client reporting its own state: the `BANNER` slot, a space,
           then the text. No template carries the slot literally.
  plain    content: tables, lists, headings, prompts the operator answers.

Severity is colour, never a word. `WARN` paints in light yellow and `GRAVE`
in light red, and only the holes: the values that tell the operator what
happened. A message without holes is painted whole. `say` paints only a stream
that is a terminal.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from string import Formatter
from typing import Any

from . import colors as C

# The banner slot: what starts every line the client says about its own state.
BANNER = "***"

INFO = "info"
WARN = "warn"
GRAVE = "grave"

SEVERITY_COLOR = {INFO: "", WARN: C.BR_YELLOW, GRAVE: C.BR_RED}

_FORMATTER = Formatter()


@dataclass(frozen=True)
class Message:
    template: str
    severity: str = INFO
    banner: bool = True

    def text(self, **fields: Any) -> str:
        """The message with its holes filled: no slot, no colour. For a
        channel that is not the screen, or that adds the slot itself."""
        return self.template.format(**fields)

    def line(self, *, colour: bool = True, **fields: Any) -> str:
        """The message as the screen shows it. `colour=False` leaves the
        severity paint off."""
        body = _paint(self.template, fields, SEVERITY_COLOR[self.severity] if colour else "")
        return banner(body) if self.banner else body

    def said(self, **fields: Any) -> Said:
        """`text()` that keeps its entry, so a printer shows it with this
        entry's slot and severity."""
        return Said(self, fields)


class Said(str):
    """A filled message. It is its text; `message` and `fields` are what
    produced it."""

    message: Message
    fields: dict[str, Any]

    def __new__(cls, message: Message, fields: dict[str, Any]) -> Said:
        said = super().__new__(cls, message.text(**fields))
        said.message = message
        said.fields = fields
        return said

    def __getnewargs__(self) -> tuple[Message, dict[str, Any]]:
        return self.message, self.fields


def banner(text: str) -> str:
    """`text` behind the banner slot."""
    return f"{BANNER} {text}"


def _paint(template: str, fields: dict[str, Any], color: str) -> str:
    if not color:
        return template.format(**fields)
    parts: list[str] = []
    holes = 0
    for literal, name, spec, conversion in _FORMATTER.parse(template):
        parts.append(literal)
        if name is None:
            continue
        holes += 1
        value = _FORMATTER.get_field(name, (), fields)[0]
        value = _FORMATTER.convert_field(value, conversion)
        spec = _FORMATTER.vformat(spec or "", (), fields)
        parts.append(f"{color}{format(value, spec)}{C.RESET}")
    if holes == 0:
        return f"{color}{template.format(**fields)}{C.RESET}"
    return "".join(parts)


def _is_terminal(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def line_for(message: Message, /, *, file: Any = None, **fields: Any) -> str:
    """The line `say` prints to `file`, stdout by default."""
    stream = file if file is not None else sys.stdout
    return message.line(colour=_is_terminal(stream), **fields)


def say(message: Message, /, *, file: Any = None, flush: bool = False, **fields: Any) -> None:
    """Print `message` to stdout, or to `file`."""
    stream = file if file is not None else sys.stdout
    print(line_for(message, file=stream, **fields), file=stream, flush=flush)


def say_said(said: Said, /, *, file: Any = None, flush: bool = False) -> None:
    """Print a message `said()` made."""
    say(said.message, file=file, flush=flush, **said.fields)


def warn(message: Message, /, **fields: Any) -> None:
    """Print `message` to stderr."""
    say(message, file=sys.stderr, flush=True, **fields)


class ArgumentParser(argparse.ArgumentParser):
    """argparse with its headings and its refusal in entries. A bad command
    line prints the usage, then ARGUMENTS_REFUSED on stderr, and exits 2."""

    def __init__(self, *args: Any, add_help: bool = True, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", _HelpFormatter)
        super().__init__(*args, add_help=False, **kwargs)
        self._positionals.title = ARGS_POSITIONALS.text()
        self._optionals.title = ARGS_OPTIONS.text()
        if add_help:
            self.add_argument("-h", "--help", action="help", help=OPT_HELP_THIS.text())

    def error(self, message: str) -> Any:
        self.print_usage(sys.stderr)
        warn(ARGUMENTS_REFUSED, error=message)
        self.exit(2)


class _HelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage: Any, actions: Any, groups: Any, prefix: str | None = None) -> None:
        super().add_usage(usage, actions, groups, ARGS_USAGE_PREFIX.text() if prefix is None else prefix)


def plural(n: int, word: str) -> str:
    """`n word`, with an s when n is not one."""
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------------------
# The entries. Grouped by where the operator meets them.
# ---------------------------------------------------------------------------

# Any failure whose text is already a whole sentence (an exception, a returned error).
FAILED = Message("{error}", GRAVE)
FAILED_WARN = Message("{error}", WARN)
# A command's refusal that is not an entry of its own: a settings verb's
# complaint, or a script line's.
COMMAND_REFUSED = Message("{error}")

# --- Session -----------------------------------------------------------------

RESUME_HINT = Message("Resume: {command}")

# --- Tool surface ------------------------------------------------------------

TOOL_SURFACE_KEPT = Message("{error}. Tool surface unchanged.", WARN)

# --- Compaction --------------------------------------------------------------

# compact_now's result, as `said()`: compaction.compacted() tells a compaction
# from a skip by the entry.
COMPACTED = Message("Compacted. Kept the tail from message {keep_from}/{total}. Summary by {model}.")
COMPACT_SKIPPED_NO_PREFIX = Message("Compaction skipped: no new prefix to summarize.")
COMPACT_SKIPPED_SAVINGS = Message("Compaction skipped: saves {savings} tokens, needs {required}.")
COMPACTION_DONE = Message("{result}")
AUTO_COMPACT_ARMED = Message("Context {fullness:.0%} full. Auto-compaction armed.", WARN)
AUTO_COMPACT_PAUSED = Message(
    "Auto-compaction paused after two compactions in a row. It resumes when context drops below the trigger.", WARN)
SUMMARY_SPLIT = Message("Summary too large. Summarizing both halves, depth {depth}.", WARN)
CLEARING_FLIGHT_FAILED = Message("Tool-result clearing: flight log not opened: {error}", GRAVE)

# --- Model and provider ------------------------------------------------------

MODEL_SET = Message("Model: {model}")
MODEL_SAVED_AS_DEFAULT = Message("Default model: {model}")
MODEL_NOT_SAVED_AS_DEFAULT = Message("Model: {model}. Not saved as default: {error}", GRAVE)
MODELS_LIMIT_NOT_A_NUMBER = Message("/models: {value!r} is not a number.")
NO_PROVIDER = Message("No provider set. Use /provider <id> first.")
MODELS_NOT_LISTED = Message("Models not listed: {error}", GRAVE)
MODEL_ROW = Message("  {model}", banner=False)
MORE_ROWS = Message("{count} more.", banner=False)
PROVIDER_IS = Message("Provider: {provider}")
PROVIDER_UNSET = Message("Provider: unset. The AI Gateway or the model prefix decides.")
PROVIDER_SET = Message("Provider: {provider}")
PROVIDER_CLEARED = Message("Provider credentials cleared for this session.")
BASE_URL_SET = Message("Base URL: set")
BASE_URL_CLEARED = Message("Base URL: cleared")
API_KEY_SET = Message("API key: set")
API_KEY_CLEARED = Message("API key: cleared")
LOGIN_UNKNOWN_PROVIDER = Message(
    "/login: {name!r} is not a known provider. Name the provider type: /login {name} <apikey> <baseurl> <provider>")
LOGIN_SAVED = Message("Login saved and active: {provider}")
CATALOG_REFRESH_FAILED = Message("Model catalog not refreshed: {error}", GRAVE)
CATALOG_REFRESHED = Message("models.dev catalog refreshed at {stamp}: {providers} providers, {models} models. {path}")

# --- Commands ----------------------------------------------------------------

USAGE = Message("Usage: {usage}")

# --- Config migration --------------------------------------------------------

MIGRATE_NO_LEGACY = Message("No legacy config at {path}", GRAVE)
MIGRATE_TARGET_EXISTS = Message("{path} already exists. Remove it first to migrate again.", GRAVE)
MIGRATE_WROTE = Message("Wrote {path} from {legacy}. Review it, then delete {legacy}. This flag is removed in 2 releases.")
SAVE_FAILED = Message("Not saved: {error}", GRAVE)
SAVED = Message("Saved {lines} to {path}")
SAVED_WITH_BACKUP = Message("Saved {lines} to {path}. Prior file backed up to {backup}")
CONTEXT_WINDOW_SET = Message("Context window: {window} for {model} from the next request.")
NO_ALIASES = Message("No aliases.", banner=False)
ALIAS_ROW = Message("alias {name} {body}", banner=False)
NO_ALIAS = Message("No alias {name}.")
ALIAS_IS_COMMAND = Message("alias {name}: {name} is a command.")
LOAD_TOO_DEEP = Message("load: nesting too deep.")
LOAD_CYCLE = Message("load: cycle at {path}")
LOAD_NOT_FOUND = Message("load: no script at {path}")
LOAD_UNREADABLE = Message("load: {path} not read: {error}", GRAVE)
LOAD_NEEDS_ONE_PATH = Message("load needs exactly one path.")
SCRIPT_LINE_FAILED = Message("{path}:{lineno}: {error}")
LOADED = Message("Loaded {path}")
RESET_DONE = Message("Cleared in-process. Session log preserved.")
WIPE_ROTATED = Message("Memory rotated to {path}")
WIPE_NOTHING = Message("No memory file to rotate.")
PERSONA_TRUNCATED = Message("Truncated. {size} bytes in all.")
BLOCKING_NO_JOBS = Message("{command} is unavailable under --blocking.")
NO_JOBS = Message("No running jobs.", banner=False)
JOB_ROW = Message("[{id}] {kind}{label}", banner=False)
NO_JOB_TO_CANCEL = Message("No matching job to cancel.")
CANCELLING = Message("Cancelling {jobs}", WARN)
NO_SUCH_SKILL = Message("No skill named {name!r}.")
SKILL_IS_A_TURN = Message("skill <name> [request] is a turn. Type it at the input line.")
HELP_HEADING = Message("Commands:", banner=False)
HELP_ROW = Message("  {usage} {doc}", banner=False)
HELP_ALIAS = Message("Alias: {body}")
HELP_ATTACH = Message("Attach a file or image to that turn. Quote a path with spaces.")
HELP_EXIT = Message("Quit.")
TURNS_COUNT = Message("{messages} in context.")
COST_NONE = Message("No model calls in this session yet.")
COST_TOTAL = Message("Session: {calls}, {cost}.")
COST_TOKENS = Message(
    "  input {input}: cache read {cache_read}, cache write {cache_write}. "
    "Output {output}: reasoning {reasoning}.", banner=False)
COST_MODEL = Message("  {model}: {calls}, {tokens} tokens, {cost}", banner=False)
COST_UNPRICED = Message("no price")
COST_PARTLY_PRICED = Message("{cost} + {calls} with no price")
SESSION_TITLED = Message("Session titled: {title}")
SESSION_TITLE = Message("{title}", banner=False)
SESSION_UNTITLED = Message("This session has no title. /name <text> gives it one.")
SESSION_NOT_SAVED_TITLE = Message("This session is not saved. Nothing to title.", WARN)
CWD_IS = Message("{path}", banner=False)
CD_DONE = Message("Working directory: {path}")
CD_NOT_A_DIR = Message("/cd: not a directory: {path}")
CD_OUTSIDE_JAIL = Message("/cd: {path} is outside the jail at {root}; /add it first.")
NO_JAIL = Message("There is no jail; /{verb} works under js -C DIR.")
ADD_BAD = Message("/add: {error}")
ADD_MISSING = Message("/add: no such path: {path}")
ADD_DONE = Message(
    "{path} is visible in the jail, {access}, from the next tool call. A running kernel or "
    "terminal session sees it after a restart.")
DROP_ROOT = Message("/drop: {path} is the -C root and cannot be dropped.")
DROP_UNKNOWN = Message("/drop: {path} was not added with /add.")
DROP_CWD = Message("/drop: the working directory is inside {path}; /cd out of it first.")
DROP_DONE = Message("{path} is no longer visible in the jail.")
NO_QUEUED_PROMPTS = Message("No queued prompts.")
ALIAS_TOO_DEEP = Message("alias {name}: nesting too deep.")
ALIAS_UNKNOWN_COMMAND = Message("alias {name}: unknown command {verb}")
TURN_RUNNING = Message("Turn running. {command} would clobber its context. ^C to cancel, or wait.")
UNKNOWN_COMMAND = Message("Unknown command: {verb}")

# --- /help: what each command does -------------------------------------------

CMD_HELP = Message("Show this message.")
CMD_SET = Message("List settings, show one, or change one. set -key clears one.")
CMD_SHOW = Message("List every setting and its effective value.")
CMD_SAVE = Message("Rewrite the global jsrc from the live settings, handlers and aliases.")
CMD_LOAD = Message("Run each line of a file as a command. source is the same command.")
CMD_ON = Message("List or register an event handler.")
CMD_ALIAS = Message("List, show or define a command alias. alias -name removes one.")
CMD_MODEL = Message("Switch model for this session. Bare opens the picker.")
CMD_PICK_MODEL = Message("Open the interactive provider and model picker.")
CMD_PROVIDER = Message("Show or switch the provider for this session.")
CMD_BASEURL = Message("Set the provider base URL for this session. Bare clears it.")
CMD_APIKEY = Message("Set the provider API key for this session. Bare clears it.")
CMD_LOGIN = Message("Load or save a provider login.")
CMD_LOGOUT = Message("Clear provider credentials for this session.")
CMD_MODELS = Message("List the provider's models.")
CMD_RESET = Message("Clear the conversation in-process. The session log keeps it.")
CMD_WIPE = Message("Rotate the session file away and clear the conversation.")
CMD_PERSONA = Message("Print the system prompt.")
CMD_TOOLS = Message("Show each tool's state and the entry that decided it. A state is eager, lazy or ban.")
CMD_SKILL = Message(
    "List skills. With a name, send that skill's instructions with the request. User-only skills work too.")
CMD_TURNS = Message("Count the messages in context.")
CMD_COST = Message("Show the session's tokens and cost, in total and by model.")
CMD_SESSION = Message("Open the session picker. Words after it start a search.")
CMD_NAME = Message("Pin a title on this session, or print the one it has.")
CMD_JOBS = Message("List running turns and subagents.")
CMD_CANCEL = Message("Cancel a job by id, or the active turn.")
CMD_FLUSH = Message("Drop all prompts queued behind the active turn.")
CMD_COMPACT = Message("Append a compaction summary mark.")
CMD_COMPACT_AUTO = Message("Turn auto-compaction on or off.")
CMD_REFRESH_MODEL_CATALOG = Message("Force-refresh the local models.dev catalog now.")
CMD_QUIT = Message("Quit. A note is kept for the next turn.")
CMD_CD = Message(
    "Change the session's working directory; with no argument, print it. Under -C, only to DIR "
    "or a bound path.")
CMD_ADD = Message("Under -C, show PATH in the jail: read-only, or read-write with :rw.")
CMD_DROP = Message("Under -C, stop showing a path added with /add.")

# --- js --help ----------------------------------------------------------------

SHORT_HELP = Message(
    "js — one agent, one terminal.\n"
    "\n"
    "RUN\n"
    "  js                          Interactive REPL in this directory.\n"
    "  js -p \"task\"                One prompt. Prints the answer.\n"
    "  echo task | js -p           The same, from stdin.\n"
    "  js -C DIR ...               Run as if launched from DIR.\n"
    "\n"
    "PICK\n"
    "  -a NAME     Agent profile: ~/.js/agents/NAME\n"
    "  -m MODEL    Provider/model, e.g. openai-codex/gpt-5.6-sol\n"
    "  -r EFFORT   off|minimal|low|medium|high|xhigh|max\n"
    "\n"
    "STATE: sessions are saved by default. A driven agent normally wants that.\n"
    "  -s NAME     Create or resume a named session.\n"
    "  --session-key KEY   Derive a stable session from agent, cwd and key.\n"
    "  --list [--json]     List every saved session.\n"
    "  -n, --no-save\n"
    "              Expensive throwaway choice: resume is unavailable, so the next\n"
    "              run must re-read context. Use only for throwaway one-liners.\n"
    "  --debug-file PATH   Full request trace, for debugging js.\n"
    "\n"
    "SCRIPTING\n"
    "  -q          No resume hint after the answer. The session is still saved.\n"
    "  -f PATH     Attach a file or image. Repeatable.\n"
    "  --max-out N Max output tokens.\n"
    "  --extra K=V One-off config, e.g. --extra limits.task_max_depth=3\n"
    "\n"
    "MORE\n"
    "  js --login [PROVIDER]   Sign in.\n"
    "  js --list-models        List the runnable models.\n"
    "  js --commit             Run the commit agent.\n"
    "  js --help-full          Every option.\n",
    banner=False)

# argparse: its headings, its refusal, and each option's help.
ARGS_USAGE_PREFIX = Message("Usage: ")
ARGS_POSITIONALS = Message("Arguments")
ARGS_OPTIONS = Message("Options")
ARGUMENTS_REFUSED = Message("{error}", GRAVE)
OPT_HELP_THIS = Message("Show this help and exit.")
OPT_HELP = Message("Show the short usage guide and exit.")
OPT_HELP_FULL = Message("Show the complete option reference and exit.")
OPT_LOGIN = Message("Interactive provider login. Without PROVIDER it lists the providers.")
OPT_LOGOUT = Message("Remove a saved provider login.")
OPT_PROMPT = Message("Run one prompt and print the final answer. Reads stdin when the value is omitted or '-'.")
OPT_FILE = Message("Attach a file or image to a one-shot prompt. Repeatable. '-' reads stdin bytes.")
OPT_AGENT = Message(
    "Internal agent id. Sessions live in ~/.js/sessions/<start dir>, runtime state in ~/.js/state/<agent>.")
OPT_MODEL = Message("Override the configured or env model for this session or prompt.")
OPT_URL = Message(
    "Reach an endpoint with no saved login in one string: model[:api-key][[shape]][[effort]]@url. "
    "Shape is openai, the default, responses or anthropic. Effort is the usual off..max ladder. "
    "The key defaults to a dummy. E.g. qwen27b@http://foo/v1  |  "
    "claude-sonnet-5:sk-foo[anthropic][max]@http://localhost:8317. "
    "Desugars to --extra model.id/provider.id/provider.base_url/provider.api_key, "
    "so an explicit --extra still wins.")
OPT_CD = Message(
    "Keep the agent in DIR, for every mode: -p, the REPL, --commit and the rest. DIR is the "
    "working directory. Every tool that starts a process runs under bubblewrap: DIR "
    "read-write, the system read-only, /home hidden except PATH directories and jail.bind "
    "entries, a private /tmp, the network on. File tools refuse paths outside DIR and the "
    "bound paths. Needs bwrap.")
OPT_DEBUG = Message(
    "In prompt and --bench modes, stream the concise per-turn diagnostics and the answer live to the terminal: "
    "run header, tool-call lines, per-call timing. The full request trace still goes only to the debug "
    "autolog file.")
OPT_DEBUG_FILE = Message(
    "Also write the full byte-honest request trace to PATH: unclipped system prompt, full tool-schema JSON "
    "with descriptions, the messages sent each call, and per-call timings. The clean final answer still "
    "prints to stdout. runtime.debug_autolog always writes the same trace under logs/<agent>/<session>.log.")
OPT_SESSION = Message(
    "Create or resume a named session: this directory's folder first, then every folder under ~/.js/sessions."
    " Bare, open the session picker.")
OPT_SESSION_KEY = Message("Derive a stable session name from agent, cwd and caller key.")
OPT_NO_SAVE = Message(
    "Expensive throwaway prompt or pipe run. Nothing is saved, resume is unavailable, "
    "and the next run must re-read context.")
OPT_QUIET = Message("Suppress the resume hint after a one-shot prompt.")
OPT_REASONING = Message(
    "Thinking effort: off|minimal|low|medium|high|xhigh|max. off disables thinking. Any other value is rejected.")
OPT_MAX_OUT = Message("Max output tokens per call.")
OPT_BENCH = Message(
    "Benchmark mode: run AGENT's NN-benchmark.md turns each on a clean slate without a session, "
    "measuring TTFT, tok/s and turn time. Pair with --stats-json or --stats-csv.")
OPT_STATS_JSON = Message("Write per-turn stats to PATH as JSON: ttft, tok/s, turn time, tokens.")
OPT_STATS_CSV = Message("Write per-turn stats to PATH as CSV.")
OPT_BLOCKING = Message(
    "Run the legacy blocking REPL: input waits for the turn to finish, and ^C exits. The default runs one "
    "async event loop, so input stays live while a turn streams and subagents run, and ^C cancels the "
    "active turn.")
OPT_EXTRA = Message(
    "Set a dotted config key for this run, e.g. --extra limits.task_max_depth=3. May be repeated. "
    "Wins over env and all config files.")
OPT_PRESET = Message(
    "Layer jsrc.<name> preset files on top of the base config, in order. The last wins. "
    "Comma-list and/or repeatable: --preset fast,debug. Looks for jsrc.<name> beside the global jsrc "
    "and in project .js/. Still below env and --extra.")
OPT_IGNORE_LOCAL = Message("Ignore project .js/jsrc and .js/jsrc.local.")
OPT_IGNORE_GLOBAL = Message("Ignore the platform jsrc.")
OPT_MIGRATE_CONFIG = Message("One-shot: convert a legacy config.toml to jsrc, then exit.")
OPT_LIST = Message("List saved sessions without loading config or contacting a provider.")
OPT_LAST = Message("Resume the most recently used session for this agent.")
OPT_JSON = Message("With -p or pipe mode, print the run as JSON events one per line; "
                   "with --list, print compact JSON objects one per line.")
OPT_PROVIDERS_JSON = Message("Print the provider registry as JSON for external pickers.")
OPT_LOGINS_JSON = Message("Print saved logins as JSON for external pickers.")
OPT_MODELS_JSON = Message("Print cached or live models for PROVIDER as JSON.")
OPT_LIST_MODELS = Message("Print human-readable models for PROVIDER and the exact --model values to pass.")
OPT_REFRESH_MODEL_CATALOG = Message("Force-refresh js's local models.dev catalog now.")
OPT_COMMIT = Message(
    "Run the built-in commit agent against the target dir, the cwd by default. A missing repo is initialized.")
OPT_COMPACT = Message("Compact an existing session id or path offline, append-only.")
OPT_IM_A_PUSSY = Message(
    "Opt out of inline-code execution for this run: !{{sh|python|c|node ...}} directives and ```!lang fences "
    "stay as written instead of running. Inline code runs by default. Set runtime.allow_inline_code off or "
    "JS_ALLOW_INLINE_CODE=0 to make that permanent. {{{{VAR}}}} env expansion and !{{env}}/!{{file}} are "
    "always on.")
OPT_PRINTONLY = Message(
    "Dry run: assemble what would be sent and print it instead of calling the model, then exit. "
    "LETTERS pick sections: t=tools p=prompt e=env-expanded i=inlines-expanded b=benchmark a=everything. "
    "The default is a. An optional :COUNT caps output lines. An optional :PATH writes to a file instead of "
    "stdout, and an empty slot skips, e.g. p::/tmp/x.md. Unknown letters and unwritable paths are reported "
    "and skipped. The run still succeeds.")
OPT_TARGET = Message("Target path for built-in commit mode.")

# `python -m js.home`, `python -m js.toolstats`, `python -m js.tooldiag`.
HOME_DESCRIPTION = Message("Move the pre-~/.js locations into ~/.js.")
OPT_HOME_APPLY = Message("Move. Without it, a dry run.")
TOOLSTATS_DESCRIPTION = Message(
    "Summarize one js session's tool traffic as a single JSON object: calls per tool, results that came "
    "back as errors, assistant turns, bytes of arguments and results, and shell commands that reached for "
    "a plain Unix tool where a dedicated tool may exist.")
OPT_TOOLSTATS_PATH = Message("Session JSONL to summarize.")
OPT_TOOLSTATS_LATEST = Message("Summarize the newest session instead of a path.")
OPT_TOOLSTATS_DATA_DIR = Message("Directory holding sessions/. The js home by default.")
OPT_TOOLSTATS_AGENT = Message("Restrict --latest to this agent's sessions.")
OPT_TOOLSTATS_TAG = Message("Prefix the JSON line with this word, e.g. TOOLSTATS.")
OPT_TOOLSTATS_EXTRA = Message("Extra key=value to include. Repeatable.")
TOOLSTATS_NEEDS_PATH = Message("Give a session path or --latest.")
TOOLDIAG_DESCRIPTION = Message("Per-tool byte cost of model-facing descriptions and parameter schemas.")
OPT_TOOLDIAG_SURFACE = Message("Comma-separated tool names. The full default registry by default.")

# --- js --list ------------------------------------------------------------------

LIST_HEADINGS = Message("AGENT NAME MTIME SIZE TURNS IN-FLIGHT CWD JOB")
LIST_YES = Message("yes")
LIST_NO = Message("no")

# --- One-shot and startup ----------------------------------------------------

BAD_REASONING = Message("--reasoning {value}: {error}", GRAVE)
PROMPT_EMPTY = Message("Prompt is empty.", GRAVE)
TURN_INTERRUPTED = Message("Turn interrupted.", WARN)
TURN_INTERRUPTED_KEPT = Message("Turn interrupted. Partial work kept.", WARN)
TURN_ABORTED = Message("Turn aborted.", WARN)
NO_ASSISTANT_RESPONSE = Message("No assistant response.", GRAVE)
CONTINUE_HINT = Message("Continue: {command}")

# --- Benchmarks --------------------------------------------------------------

NO_BENCHMARKS = Message("Agent {agent!r} has no NN-benchmark.md files.", GRAVE)
BENCH_START = Message("bench {name}: {prompt}")
BENCH_FAILED = Message("{name}: {error}", GRAVE)
BENCH_ROW = Message("{name}: {tokens} tok  {tps:.1f} tok/s  ttft {ttft}  wall {wall:.2f}s")
STATS_WRITTEN = Message("Stats: {path}")

# --- Commit agent ------------------------------------------------------------

COMMIT_TARGET_MISSING = Message("Commit target does not exist: {path}", GRAVE)
COMMIT_TARGET_NOT_DIR = Message("Commit target is not a directory: {path}", GRAVE)
GIT_INIT_FAILED = Message("git init failed in {path}: {error}", GRAVE)
SNAPSHOT_FAILED = Message("Worktree snapshot failed: {error}", WARN)
SNAPSHOT_SAVED = Message("Worktree snapshot saved: {path}")
SURVEY_FAILED = Message("Commit survey failed for {path}: {error}", GRAVE)
SURVEY_EXITED = Message("commit_helper survey exited {code}")

# --- Model listing, presets, binaries ---------------------------------------

NO_LOGINS = Message("No providers logged in. Run js --login <provider>.")
MODEL_LIST_FAILED = Message("{provider}: models not listed: {error}", WARN)
PRESET_NOT_FOUND = Message("--preset {name}: no jsrc.{name}. Looked in {looked}.", WARN)
PRESET_NO_CONFIG_DIRS = Message("--preset {name}: no jsrc.{name}. Every config dir is ignored.", WARN)
BINARY_MISSING = Message("{name} not in tools/bin or PATH. {user} uses it. just install provisions it.", WARN)

# --- The REPL ----------------------------------------------------------------

INPUT_PROMPT = "LO> "
NO_SKILLS = Message("No skills found.")
SKILL_ROW = Message("{name} {source} {description}", banner=False)
SKILL_ROW_USER_ONLY = Message("{name} user-only {source} {description}", banner=False)
SKILL_HINT = Message("/skill <name> [request] sends one with your request.", banner=False)
DROPPED_QUEUED = Message("Dropped {prompts}.", WARN)
QUEUED = Message("Queued. {ahead} ahead.")
PRESS_CTRL_C_AGAIN = Message("Press ^C again to exit.")

# --- --printonly -------------------------------------------------------------

PRINTONLY_BAD_COUNT = Message("--printonly: {count!r} is not a count. Ignored.", WARN)
PRINTONLY_LETTER_NOT_IMPLEMENTED = Message("--printonly: {letter!r} is not implemented. Skipped.", WARN)
PRINTONLY_UNKNOWN_LETTER = Message("--printonly: unknown letter {letter!r}. Skipped.", WARN)
PRINTONLY_NO_CONFIG = Message("--printonly: config not built: {error}", GRAVE)
PRINTONLY_NO_PROMPT = Message("--printonly: prompt not loaded: {error}", GRAVE)
PRINTONLY_NO_BENCHMARKS = Message("No NN-benchmark.md files.")
PRINTONLY_SECTION_FAILED = Message("--printonly: section {section} unavailable: {error}", GRAVE)
PRINTONLY_NOT_WRITTEN = Message("--printonly: {path} not written: {error}. Printing to stdout.", WARN)

# --- Command-line flags ------------------------------------------------------

BAD_URL_SPEC = Message("-u: {error}", GRAVE)
JAIL_REFUSED = Message("{error}", GRAVE)
JSON_NEEDS_LIST = Message("--json requires -p, pipe mode or --list.", GRAVE)
LIST_EXCLUSIVE = Message("--list does not combine with run or session options.", GRAVE)
LAST_WITH_SESSION = Message("--last and --session are mutually exclusive.", GRAVE)
NO_PREVIOUS_SESSION = Message("No previous session for agent: {agent}", GRAVE)
DEBUG_FLAGS_EXCLUSIVE = Message("--debug and --debug-file are mutually exclusive.", GRAVE)
MODES_EXCLUSIVE = Message("--commit and --compact are mutually exclusive.", GRAVE)
FILE_NEEDS_PROMPT_MODE = Message("-f/--file works only with a prompt or a pipe. Use @path in the REPL.", GRAVE)
FILE_NEEDS_PROMPT = Message("-f/--file requires -p/--prompt or piped input. Use @path in the REPL.", GRAVE)
BENCH_EXCLUSIVE = Message("--bench is its own mode. Name the agent as --bench AGENT, without --agent or a built-in mode.", GRAVE)
COMMIT_WITH_AGENT = Message("--commit always uses the built-in commit agent. Omit --agent.", GRAVE)
STDIN_TWICE = Message("stdin cannot be both the prompt and an attachment.", GRAVE)
STDIN_ATTACHMENT_NOT_PIPED = Message("-f - requires piped stdin bytes.", GRAVE)
# Raised as exception text and shown through FAILED.
ATTACHMENT_NOT_FOUND = Message("Attachment not found: {path}")
ATTACHMENT_NOT_A_FILE = Message("Attachment is not a regular file: {path}")
ATTACHMENT_UNREADABLE = Message("Attachment {path} not read: {error}")
ATTACHMENT_IMAGE_TOO_LARGE = Message("Image attachment {path} is {size} bytes. The maximum is {limit} bytes.")
# The paste-image key (js.clipimage): one line, and the input line is unchanged.
CLIPBOARD_NONE = Message("No clipboard: no Wayland or X11 display. ui.paste_image_command names a command that prints the image.", WARN)
CLIPBOARD_TOOL_MISSING = Message("Clipboard image not read: {tool} is not installed.", WARN)
CLIPBOARD_READ_FAILED = Message("Clipboard image not read: {tool}: {error}", WARN)
CLIPBOARD_NO_IMAGE = Message("No image on the clipboard.", WARN)
CLIPBOARD_IMAGE_TOO_LARGE = Message("Clipboard image is {size} bytes. The maximum is {limit} bytes.", WARN)
SESSION_NAME_UNSAFE = Message("Session name is not a safe relative path: {session}")
SESSION_NAME_TRAVERSAL = Message("Session name has an empty or traversal component: {session}")
SESSION_NAME_ABSOLUTE = Message("Session name is not a relative path: {session}")
SESSION_NAME_SUFFIX = Message("Session name has a suffix other than .jsonl: {session}")
NOT_SAVED_NO_RESUME = Message("Session not saved. Resume unavailable.", WARN)

# --- REPL startup ------------------------------------------------------------

STARTUP = Message(
    f"{C.CYAN}me — js agent{C.RESET}\n"
    f"{C.MAGENTA}Agent:{C.RESET}  {{agent}}\n"
    f"{C.MAGENTA}Model:{C.RESET}  {{model}}\n"
    f"{C.MAGENTA}Prompt:{C.RESET} {{prompt}}\n"
    f"{C.MAGENTA}Memory:{C.RESET} {{memory}}\n"
    "\n"
    f"{C.GREEN}Type exit or Ctrl-D to quit. /help lists commands.{C.RESET}\n",
    banner=False,
)
RESUMED_MODEL = Message("Model: {model}")
RESUMED = Message("Resumed: {messages}.")
EMPTY_SESSION = Message("Empty session. Nothing to resume: {path}", WARN)
PROMPT_CHANGED = Message("Agent prompt changed on disk. Session keeps the one it started with.")
NO_SUCH_AGENT = Message(
    "No such agent: {agent}. Looked in project .js/agents, {agents_dir}, and repo prompts. "
    "Create {new_dir}/ with NN-*.md prompt files and an optional agent.yaml manifest.", GRAVE)

# --- A turn ------------------------------------------------------------------

# Chrome: the turn's own report lines. The caller paints them.
RUN_LINE = Message("run  {fields}", banner=False)
CALL_STATS = Message("{ms}ms  finish={finish}  tool_calls={tool_calls}  {tokens} tok  {tps:.1f} tok/s{ttft}{cache}",
                     banner=False)
RETRY_BUDGET_EXHAUSTED = Message("Tool-loop retry budget exhausted.", GRAVE)
MAX_ITERATIONS = Message("Tool loop hit max iterations: {limit}", GRAVE)
RESPONSE_INCOMPLETE = Message("Response incomplete: {reason}", WARN)
STEERED = Message("Steered.")
FLIGHT_LOG_FAILED = Message("Flight log not written: {error}", GRAVE)
COMPACTION_FAILED = Message("Compaction failed: {error}", GRAVE)

# --- The network channel -----------------------------------------------------

NET_CONNECTING = Message("Connecting: {url}")
NET_CONNECTED = Message("Connected: {host}  {ms}ms")
NET_ROLE_CONNECTING = Message("{role}: connecting {url}")
NET_ROLE_CONNECTING_AGENT = Message("{role}: connecting {url}  agent={agent}")
NET_ROLE_CONNECTED = Message("{role}: connected: {host}  {ms}ms")
NET_COMPACTING = Message("Compacting: {model} via {url}")
NET_DNS_FAILURE = Message("DNS failure")
NET_TIMEOUT = Message("Timeout")
NET_CONNECT_FAILED = Message("Connection failed")
CATALOG_UPDATING = Message("Updating the models.dev cache.")
CATALOG_UPDATE_FAILED = Message("models.dev cache not refreshed: {error}", WARN)

# --- Login -------------------------------------------------------------------

BUILTIN_VALUE = Message("default")
NONE_VALUE = Message("none")
PICKER_NO_MATCHES = Message("No matches.")
PICKER_SELECTED = Message("{selected}/{total} selected")
PICKER_KEYS = Message("↑↓/jk move  pgup/pgdn page  / search  enter select  q/esc back")
PICKER_KEYS_CHECKLIST = Message("↑↓/jk move  / search  space toggle  a all  n none  enter save  q back")
PICKER_KEYS_SEARCH = Message("Search: type to filter, enter to navigate, esc clear")
LOGIN_PICK_MODELS = Message("Models to keep for {provider}. Kept for /model and --list-models.")
LOGIN_ASK_EXTRA_MODELS = Message("Model ids the list missed, comma-separated. Enter skips")
LOGIN_ADD_CUSTOM = Message("<add custom provider>")
LOGIN_ADD_REGISTRY = Message("<add registry provider>")
LOGIN_PICK_PROVIDER = Message("Select provider")
LOGIN_PICK_SHAPE = Message("Select API shape")
LOGIN_ASK_CUSTOM_ID = Message("Custom provider id")
LOGIN_ENV_MODEL = Message("Preferred model from env: {model}")
LOGIN_ASK_BASE_URL = Message("Base URL")
LOGIN_ASK_ENV_KEY = Message("Found ENV:{name} {key}. Use it? [y/N]")
LOGIN_ASK_KEY = Message("Enter API key")
LOGIN_ASK_KEY_OPTIONAL = Message("Enter API key. Enter for none")
LOGIN_ASK_KEY_KEEP = Message("Enter API key. Enter keeps the saved one")
LOGIN_NO_KEY = Message("Login aborted. No API key given.", GRAVE)
LOGIN_PLACEHOLDER_KEY = Message("No key given. Storing placeholder 'x'; local endpoints ignore it.")
LOGIN_ASK_HEADERS = Message("Headers k=v,k=v, optional")
LOGIN_MODEL_ROW = Message("[{index}] {model}", banner=False)
LOGIN_LISTING_NOT_PROOF = Message("Model listing alone does not prove these credentials can generate.")
LOGIN_ASK_TEST = Message("[enter] adds without a test. A model number verifies it first. q cancels: ")
LOGIN_TEST_CHOICES = Message("Enter adds, q cancels, a model number or exact model id tests it.")
LOGIN_TEST_USER = Message("[user] {prompt}")
LOGIN_TEST_FAILED = Message("Secondary test failed: {error}", GRAVE)
LOGIN_TEST_ANSWER = Message("[assistant] {answer}")
LOGIN_DETAIL_BASE_URL = Message("Base URL: {url}")
LOGIN_DETAIL_KEY = Message("API key: {key}")
LOGIN_DETAIL_CACHED = Message("{models}")
LOGIN_DETAIL_HEADERS = Message("Headers: {headers}")
LOGIN_EDIT_BASE_URL = Message("Base URL. Enter keeps saved, - clears")
LOGIN_EDIT_KEY = Message("API key. Enter keeps saved, - clears")
LOGIN_EDIT_HEADERS = Message("Headers k=v,k=v. Enter keeps saved, - clears")
LOGIN_BAD_HEADERS = Message("Headers must use k=v,k=v. Nothing saved yet.", WARN)
LOGIN_MANAGE_PROVIDERS = Message("Manage providers")
LOGIN_PROVIDER_NOT_CHANGED = Message("Provider not changed: {error}", GRAVE)
LOGIN_MODELS_SELECT = Message("Select / deselect cached models")
LOGIN_MODELS_ADD = Message("Add model ids")
LOGIN_MODELS_REFETCH = Message("Re-fetch live model list")
LOGIN_BACK = Message("Back")
LOGIN_MODELS_TITLE = Message("Models for {provider}: {count} cached")
LOGIN_CACHE_EMPTY = Message("Cache empty. Add ids or re-fetch first.")
LOGIN_ASK_MODEL_IDS = Message("Add model ids, comma-separated")
LOGIN_FETCHING = Message("Fetching models.")
LOGIN_REFETCH_FAILED = Message("Fetch failed: {error}. Cache unchanged.")
LOGIN_UPDATE = Message("Update URL / API key / headers")
LOGIN_MODELS = Message("Models")
LOGIN_REMOVE = Message("Remove provider")
LOGIN_MANAGE_ONE = Message("Manage {provider}")
LOGIN_FAILED = Message("Login failed: {error}", GRAVE)
LOGIN_TRY_V1 = Message("{base} does not end in /v1. OpenAI-compatible servers usually serve at {suggested}", WARN)
LOGIN_NOT_SAVED = Message("Login not saved: {error}", GRAVE)
PROVIDER_ADDED = Message("Provider added: {provider}. Cached {models}.")
PROVIDER_ADDED_AS = Message("Provider added: {provider} as {email}. Cached {models}.")
LOGIN_NO_CACHED_MODELS = Message("No cached models for {provider}", GRAVE)
LOGIN_CACHE_EDIT_CANCELLED = Message("Model cache edit cancelled. {provider} unchanged.")
LOGIN_CACHED = Message("Cached {models} for {provider}")
LOGOUT_FAILED = Message("Logout failed: {error}", GRAVE)
LOGGED_OUT = Message("Logged out of {provider}")
NOT_LOGGED_IN = Message("Not logged in to {provider}", GRAVE)
OAUTH_OPENING = Message("Opening the browser for {service} login. If it does not open, visit:\n{url}")
OAUTH_PASTE_CALLBACK = Message(
    "Over SSH, or if the callback page cannot connect, paste the full callback URL here and press Enter.")
OAUTH_BAD_CALLBACK = Message("Callback URL invalid or its state does not match. Paste the URL from this login attempt.",
                             WARN)
OAUTH_DEVICE = Message("{service} login\n  URL:  {url}\n  Code: {code}\nWaiting for authorization.")
OAUTH_REFRESH_NOT_SAVED = Message("Refreshed {service} login not saved: {error}", WARN)

# --- Tool binaries -----------------------------------------------------------

TOOL_DIR = Message("Tool directory: {path}")
TOOL_PRESENT_BREW = Message("Present: {name} from Homebrew at {path}")
TOOL_PRESENT = Message("Present: {name} {version} at {path}, sha256 {sha}")
TOOL_DOWNLOAD = Message("Download: {name} {version}, {asset}\n  {url}")
TOOL_INSTALLED = Message("{state}: {path}. Asset sha256 {asset_sha}, executable sha256 {sha}")
TOOL_INSTALL_FAILED = Message("Tool install failed: {error}", GRAVE)
URLLIB_FALLBACK = Message(
    "aria2c unavailable. {purpose} falls back to urllib: no segmented transfer, no resume across attempts.", WARN)

# --- Moving into ~/.js -------------------------------------------------------

HOME_MOVED = Message("Moved {source} to {target}")
HOME_WOULD_MOVE = Message("Would move {source} to {target}")
HOME_REMOVED_DUPLICATE = Message("Removed {source}: identical to {target}")
HOME_WOULD_REMOVE_DUPLICATE = Message("Would remove {source}: identical to {target}")
HOME_REMOVED_EMPTY = Message("Removed empty {source}")
HOME_WOULD_REMOVE_EMPTY = Message("Would remove empty {source}")
HOME_REFUSED = Message("Refused {source}: {reason}", WARN)
HOME_WOULD_REFUSE = Message("Would refuse {source}: {reason}", WARN)
HOME_KIND_LINK = Message("a symlink")
HOME_KIND_DIR = Message("a directory")
HOME_KIND_FILE = Message("a file")
HOME_NOT_EXAMINED = Message("not examined: {error}")
HOME_NOT_EXAMINED_WITH = Message("it or {target} not examined: {error}")
HOME_NOT_LISTED = Message("not listed: {error}")
HOME_NOT_COMPARED = Message("not compared with {target}: {error}")
HOME_DUPLICATE_NOT_REMOVED = Message("identical to {target} but not removed: {error}")
HOME_TARGET_DIFFERS = Message("{target} already exists with different content")
HOME_TARGET_OTHER_KIND = Message("{target} already exists as {kind}. This is {source_kind}")
HOME_OLD_COPY_LEFT = Message("copied to {target}, but the old copy is left: {error}")
HOME_NOT_MOVED = Message("not moved to {target}: {error}")
HOME_NOT_A_DIR = Message("is {kind}, not a directory. Move it by hand")
HOME_MIGRATION_FAILED = Message("Move into {home} failed: {error}", GRAVE)
HOME_NOTHING_TO_MOVE = Message("Nothing to move into {home}.", banner=False)
HOME_RELINKED = Message("Relinked {source} to {target}")
HOME_WOULD_RELINK = Message("Would relink {source} to {target}")
HOME_CONVERTED = Message("Converted {source}: {reason}")
HOME_WOULD_CONVERT = Message("Would convert {source}: {reason}")
HOME_DROPPED = Message("Dropped from {source}, matching no tool: {reason}")
HOME_WOULD_DROP = Message("Would drop from {source}, matching no tool: {reason}")
HOME_LEFT = Message("Left {source}: {reason}", WARN)
HOME_WOULD_LEAVE = Message("Would leave {source}: {reason}", WARN)
HOME_REFILED = Message("Filed {reason} from {source} by start directory")
HOME_WOULD_REFILE = Message("Would file {reason} from {source} by start directory")
HOME_SESSION_IN_USE = Message("a js process has it open")
HOME_NOT_EMPTY = Message("not empty after filing its sessions")
HOME_UNUSED = Message("Left in place, js does not use it: {source}")
HOME_WOULD_LEAVE_UNUSED = Message("Would leave in place, js does not use it: {source}")

# --- Agent manifests to agent.yaml -------------------------------------------

AGENT_MIGRATE_CONVERTED = Message("{file} -> agent.yaml")
AGENT_MIGRATE_PRUNED = Message("{file} rewritten")
AGENT_MIGRATE_BOTH = Message("both agent.yaml and {file} exist; merge by hand")
AGENT_MIGRATE_ZERO_MD = Message(
    "{files} were ignored beside {file} and would load as prompt text after migration; move them first")
AGENT_MIGRATE_BAD_YAML = Message("{file}: invalid YAML: {error}")
AGENT_MIGRATE_NOT_A_MAPPING = Message("{file} is not a mapping")
AGENT_MIGRATE_UNKNOWN_KEYS = Message("{file}: unknown keys {keys}")
AGENT_MIGRATE_RESTORED = Message("restored {file}; agent.yaml did not load: {error}")
AGENT_MIGRATE_SYMLINK = Message("symlink to {target}; convert it where it lives")

# --- Session files -----------------------------------------------------------

SESSION_AMBIGUOUS = Message("Session {session} is in more than one folder: {paths}")
SESSION_NOT_RESERVED = Message("No free session name in {folder}")
SESSION_BRANCH_NO_MESSAGE = Message("{path} has no message {message}")
SESSION_RECORDS_SKIPPED = Message(
    "{path}: skipped {records} from an incompatible schema version. No migration to {version} yet. "
    "History may be incomplete.", WARN)

# --- The ex line -------------------------------------------------------------

BUFFER_WRITTEN = Message("Buffer written to {path}. Not sent.")
EX_UNKNOWN = Message("Not an editor command, js command or program: {verb}", GRAVE)
EX_FAILED = Message(":{verb}: {error}", GRAVE)

# --- Agents, tools, skills, settings at load time ----------------------------

TOOLSTATS_NO_SESSION = Message("toolstats: no session found.", GRAVE)
TOOLDIAG_NO_TOOLS = Message("tooldiag: the surface matched no tools.", GRAVE)
AGENT_DIR_UNREADABLE = Message("Agent dir {path} unreadable: {error}. Skipped.", WARN)
AGENT_NAME_IS_TOOL = Message("Agent {agent!r}: name collides with a builtin tool. Not exposed as a tool. Rename {root}/{agent}.",
                             WARN)
TOOL_ENTRY_UNMATCHED = Message("Tool entry {entry!r} matched no tool. Ignored.", WARN)
TOOL_ENTRY_UNMATCHED_FOR = Message("Tool entry {entry!r} for agent {agent!r} matched no tool. Ignored.", WARN)
DESCRIPTION_PROBLEM = Message("description: {problem}", WARN)
DESCRIPTION_NESTED_BLOCK = Message("{label}nested {{{{#if}}}}/{{{{#unless}}}} block is unsupported. Kept as written.")
DESCRIPTION_BLOCK_NAMES_NO_TOOL = Message("{label}conditional block names no tool. Kept as written.")
DESCRIPTION_UNBALANCED = Message("{label}unbalanced {{{{#if}}}}/{{{{#unless}}}} tag. Kept as written.")
SKILL_SKIPPED = Message("Skill {path} skipped: {error}")
SKILL_DUPLICATE = Message("Skill {path} skipped: {root} already has a skill named {name!r} at {prior}")
SKILL_OVERRIDES = Message("Skill {name!r} at {path} is used in place of {prior}")
ENV_SETTING_IGNORED = Message("{name}: {error}. Ignored.", WARN)
DIRECTIVE_NOT_EXPANDED = Message("Directive not expanded: {error}", WARN)
# Why a directive was not expanded: the {error} hole of DIRECTIVE_NOT_EXPANDED.
DIRECTIVE_UNKNOWN_SUBSYSTEM = Message("Unknown inline subsystem {name!r}. Known: {known}")
DIRECTIVE_CODE_OFF = Message(
    "Inline {name!r} runs code, and inline code is off: "
    "--im-a-pussy, set runtime.allow_inline_code off or JS_ALLOW_INLINE_CODE=0")
DIRECTIVE_ENV_NEEDS_NAME = Message("!{{env ...}} needs a variable name. Got {body!r}")
DIRECTIVE_NOT_ON_PATH = Message("{label}: {program!r} not found on PATH")
DIRECTIVE_TIMED_OUT = Message("{label}: timed out after {seconds}s")
DIRECTIVE_EXITED = Message("{label}: exited {code}: {stderr}")
DIRECTIVE_EXITED_SILENT = Message("{label}: exited {code}. No stderr")
FLIGHT_AUTOLOG_FAILED = Message("Flight autolog not written: {error}. Flight {path}", GRAVE)
FLIGHT_NOTICE = Message("Compaction {event} {id}: operation={operation} {detail} flight={path}")

# --- Settings verbs ----------------------------------------------------------

UNKNOWN_SETTING = Message("Unknown setting: {key}")
EXTRA_UNKNOWN_SETTING = Message("--extra: unknown setting: {key}")
SETTING_VALUE = Message("{key} = {value}", banner=False)
SETTING_ALREADY_UNSET = Message("{key} = {value}  Already unset.", banner=False)
SETTING_LIVE = Message("{line}  live: {source}", banner=False)
NO_EVENT_HANDLERS = Message("No event handlers.", banner=False)
ON_NEEDS_TWO = Message("on needs an event and a handler.")
SET_NEEDS_TWO = Message("set needs a key and a value: {line!r}")
EXPECTED_ONE_OF = Message("expected {choices}")
EXPECTED_COLOUR = Message("expected a #rrggbb colour, got {value!r}")
EXPECTED_KEY = Message("expected prompt_toolkit key names such as c-v or escape v, got {value!r}")
EXPECTED_URL = Message("expected a URL starting with http:// or https://, got {value!r}")
EXPECTED_INTEGER = Message("expected an integer")
EXPECTED_LEVEL = Message("expected an integer from 0 to 3")
EXPECTED_POSITIVE_INTEGER = Message("expected an integer >= 1")
EXPECTED_NUMBER = Message("expected a number")
EXPECTED_POSITIVE_NUMBER = Message("expected a number > 0")
EXPECTED_JSON = Message("expected a JSON value")
EXPECTED_JSON_OBJECT = Message("expected a JSON object")
EXPECTED_ENV_NAMES = Message("expected a JSON list of non-empty environment-variable names")
EXPECTED_JSON_LIST = Message("expected a JSON list")
EXPECTED_ALIAS_PROFILES = Message("expected profiles with match and aliases")
EXPECTED_NONEMPTY_ALIASES = Message("expected non-empty aliases")
EXPECTED_NONEMPTY_MATCH = Message("expected non-empty match values")
EXPECTED_CANONICAL_TOOL_NAMES = Message("expected canonical tool names matching [A-Za-z0-9_-]+")
EXPECTED_ALIAS_NAMES = Message("expected alias names matching [A-Za-z0-9_-]+")
EXPECTED_UNIQUE_ALIASES = Message("expected unique alias names")
EXTRA_NOT_KEY_VALUE = Message("--extra expects KEY=VALUE. Got {arg!r}")
EXTRA_EMPTY_KEY = Message("--extra key is empty: {arg!r}")
EXTRA_EMPTY_VALUE = Message("--extra value is empty: {arg!r}")
EXTRA_BAD_VALUE = Message("--extra {key}: {error}")
UNKNOWN_PROVIDER_ID = Message("unknown provider id: {provider!r}. Pick a known id or add a custom one with js --login.")

# --- Provider failures -------------------------------------------------------

# Raised as exception text and shown through FAILED.
NO_PROVIDER_SET = Message("No provider set.")
PROVIDER_NOT_LOGGED_IN = Message(
    "Provider {provider!r} is not logged in. Run `js --login {provider}`. `js --list-models` lists what is runnable.")
MODEL_UNCONFIGURED = Message(
    "Model {model!r} has no provider and no login. Set provider.id or JS_PROVIDER, run `js --login`, "
    "or prefix a logged-in provider. `js --list-models` lists what is runnable.")
PROVIDER_NEEDS_KEY = Message(
    "Provider {provider!r} needs an API key. Run `js --login {provider}` or `set provider.api_key <value>`.")
PROVIDER_NO_AUTH_METHOD = Message(
    "Provider {provider!r} needs an API key: the SDK found no way to authenticate. "
    "Run `js --login {provider}` or `set provider.api_key <value>`.")
PROVIDER_UNKNOWN = Message(
    "Unknown provider {provider!r}. Run `js --login {provider}`. `js --list-models` lists what is runnable.")
PROVIDER_AUTH_FAILED = Message(
    "Provider {provider!r} authentication failed: {detail}. "
    "Run `js --login {provider}` or `set provider.api_key <value>`.")
PROVIDER_NOT_CONFIGURED = Message(
    "Provider {provider!r} is not configured: {detail}. "
    "Run `js --login {provider}` or `set provider.api_key <value>`.")

# --- Tool policy table (/tools) ----------------------------------------------

# tools.yaml and tool-chain entries that cannot be resolved: exception text.
POLICY_UNREADABLE = Message("{path}: tools.yaml not read: {error}")
POLICY_NOT_A_MAPPING = Message("{path}: tools.yaml must be a mapping of tags, ban and skills.")
POLICY_UNKNOWN_KEYS = Message("{path}: unknown keys {keys}. Allowed: tags, ban, skills.")
POLICY_TAGS_NOT_A_MAPPING = Message("{path}: tags must map a tag name to a list of entries.")
POLICY_TAG_NAME_EMPTY = Message("{path}: tag name {name!r} must be a non-empty string.")
POLICY_TAG_INTRINSIC = Message("{path}: tag {name!r} is intrinsic and cannot be redefined.")
POLICY_TAG_NOT_A_LIST = Message("{path}: tags.{name} must be a list of noun:modifier entries.")
POLICY_BAN_NOT_A_MAPPING = Message("{path}: ban must map a tool name to a list of argument patterns.")
POLICY_BAN_NOT_A_LIST = Message("{path}: ban.{tool} must be a list of non-empty strings.")
POLICY_SKILLS_NOT_A_LIST = Message("{path}: skills must be a list of family:name strings.")
POLICY_ENTRY_NOT_A_STRING = Message("{where}: tool entry {entry!r} must be a string like read:eager.")
POLICY_ENTRY_SHAPE = Message("{where}: tool entry {entry!r} is not noun:modifier, e.g. read:eager, shell:ban, tag:NAME.")
POLICY_TAG_MODIFIER = Message("{where}: tool entry {entry!r}: only an intrinsic tag takes a modifier.")
POLICY_BAD_MODIFIER = Message("{where}: tool entry {entry!r}: modifier must be eager, lazy or ban.")
POLICY_TOOLS_NOT_A_LIST = Message("{where}: tools must be a list of noun:modifier entries.")
POLICY_UNKNOWN_TAG = Message("{where}: {entry!r} names no tag in {config}. Known: {known}")
POLICY_TAG_CYCLE = Message("{config}: tag cycle {cycle}")

TOOL_CHAIN_ROW = Message("{tool:<{width}}  {state:<8}{decided}", banner=False)
TOOL_CHAIN_TOOL = Message("Tool")
TOOL_CHAIN_STATE = Message("State")
TOOL_CHAIN_DECIDED_BY = Message("Decided by")
TOOL_CHAIN_BAN = Message("ban {tool}: {patterns}", banner=False)

# --- Status bar --------------------------------------------------------------

STATUS_COMPACTING = Message("compacting")
STATUS_CACHE = Message("cache {pct}%")

# --- Tool exchanges ----------------------------------------------------------

TOOL_ARGS_UNPARSED = Message("<arguments are not a JSON object>")
TOOL_MORE_LINES = Message("... +{count} lines")

# --- The kernel panel --------------------------------------------------------

KERNEL_MORE_LINES = Message("... {count} more lines. The model got the full text.")
KERNEL_MORE_CODE_LINES = Message("... {count} more lines of code.")
KERNEL_EVENT_MORE_LINES = Message("... {count} more lines.")
KERNEL_CELL = Message("Kernel cell {cell}: ")
KERNEL_CELL_INTERRUPTED = Message("Cell interrupted.")
KERNEL_STOPPED = Message("SIGINT sent. Namespace intact.")
KERNEL_PANEL_TITLE = Message("Kernel cell {cell}")
KERNEL_STARTED = Message("Kernel started in {cwd}")
KERNEL_RESTARTED = Message("Kernel restarted. Namespace cleared.")
DEFAULTS_MISSING = Message("Defaults file missing: {path}", GRAVE)
DEFAULTS_NOT_A_SET_LINE = Message("not a set line")
DEFAULTS_BAD_LINE = Message("{location}: {error}", GRAVE)
DEFAULTS_UNLISTED = Message("{path}: no line for {keys}", GRAVE)

# --- The model picker --------------------------------------------------------

PICK_HELP = Message("tab panes • ↑↓ move • enter select • f fetch • /login adds providers • esc/q quit")
PICK_NO_LOGINS = Message("No logged-in providers. Use /login or js --login.")
PICK_NO_MODELS = Message("No cached models. Press f to fetch.")
PICK_NO_CACHED = Message("{provider}: no cached models. Press f to fetch.")
PICK_PROVIDER_DETAIL = Message("{provider} [{source}]: {models}")
PICK_NOT_LOGGED_IN = Message("{provider}: not logged in. Use /login first.")
PICK_FETCH_FAILED = Message("Fetch failed: {error}")
PICK_FETCHING = Message("Fetching…")
PICK_PROVIDERS = Message("Providers")
PICK_MODELS = Message("Models")
PICK_PROVIDER_ROW = Message("● {provider}  {name}")

# --- The session picker --------------------------------------------------------

SESSIONS_TITLE = Message(" SESSIONS  {count} · {view} · {order} · {scope}", banner=False)
SESSIONS_KEYS = Message("v=view  /=search  b=messages  i=info  a=all  esc=close", banner=False)
SESSIONS_VIEW_FLAT = Message("flat", banner=False)
SESSIONS_VIEW_DIR = Message("by dir", banner=False)
SESSIONS_VIEW_AGENT = Message("by agent", banner=False)
SESSIONS_NEWEST = Message("newest first", banner=False)
SESSIONS_RANKED = Message("best match first", banner=False)
SESSIONS_ALL_DIRS = Message("all dirs", banner=False)
SESSIONS_ALL_KINDS = Message("all dirs · every kind", banner=False)
SESSIONS_NONE = Message("No sessions match.", banner=False)
SESSIONS_BRANCH = Message("⎇ from {point}", banner=False)
SESSIONS_QUERY_KEYS = Message("enter=keep  esc=clear", banner=False)
SESSIONS_Q_WORDS = Message("words {words}", banner=False)
SESSIONS_Q_TURNS = Message("turns {test}", banner=False)
SESSIONS_Q_DATE = Message("date {date}", banner=False)
SESSIONS_Q_AGENT = Message("agent {pattern}", banner=False)
SESSIONS_Q_DIR = Message("dir {pattern}", banner=False)
SESSIONS_Q_DIR_ONE = Message("dir one level under {path}", banner=False)
SESSIONS_Q_DIR_ANY = Message("dir {path} and anywhere under it", banner=False)
SESSIONS_Q_MODE = Message("mode {mode}", banner=False)
SESSIONS_Q_MODEL = Message("model {pattern}", banner=False)
SESSIONS_Q_TAG = Message("tag {pattern}", banner=False)
SESSIONS_Q_NOTHING = Message("every session", banner=False)
SESSIONS_MESSAGES_TITLE = Message(" MESSAGES  {when} · {agent} · {dir} · {turns}", banner=False)
SESSIONS_MESSAGES_KEYS = Message("enter=branch here  r=resume at end  esc=back", banner=False)
SESSIONS_MESSAGES_NONE = Message("This session has no messages.", banner=False)
SESSIONS_INFO_TITLE = Message(" INFO  {when} · {agent} · {dir}", banner=False)
SESSIONS_INFO_KEYS = Message("enter=resume  esc=back", banner=False)
SESSIONS_INFO_ROW = Message("  {label:<12} {value}", banner=False)
SESSIONS_INFO_FILE = Message("file", banner=False)
SESSIONS_INFO_TITLE_ROW = Message("title", banner=False)
SESSIONS_INFO_AGENT = Message("agent", banner=False)
SESSIONS_INFO_DIR = Message("dir", banner=False)
SESSIONS_INFO_MODE = Message("mode", banner=False)
SESSIONS_INFO_COMMAND = Message("command", banner=False)
SESSIONS_INFO_TIMES = Message("started", banner=False)
SESSIONS_INFO_TIMES_VALUE = Message("{started}   last {last}", banner=False)
SESSIONS_INFO_SIZE = Message("size", banner=False)
SESSIONS_INFO_SIZE_VALUE = Message("{turns} · {messages} · {calls}", banner=False)
SESSIONS_INFO_MODELS = Message("models", banner=False)
SESSIONS_INFO_STAMP = Message("last stamp", banner=False)
SESSIONS_INFO_TOKENS = Message("tokens", banner=False)
SESSIONS_INFO_TOKENS_VALUE = Message("about {tokens} in the replayed history", banner=False)
SESSIONS_INFO_BRANCH = Message("branched", banner=False)
SESSIONS_INFO_BRANCH_VALUE = Message("from {parent} at {point}", banner=False)
SESSIONS_INFO_PARENT = Message("subagent of", banner=False)
SESSIONS_INFO_KIND = Message("kind", banner=False)
SESSIONS_INFO_TAGS = Message("tags", banner=False)
SESSIONS_NEEDS_TERMINAL = Message("The session picker needs a terminal. Name a session: --session NAME.", WARN)
SESSIONS_ALREADY_HERE = Message("Already in that session.")
SESSIONS_SWITCHING = Message("Switching to session {name} in {dir}")
SESSIONS_DIR_GONE = Message("The session's directory {dir} is gone. Resuming in {cwd}.", WARN)
SESSIONS_BRANCHED = Message("Branched {parent} at {point} into {name}")

# --- The commit helper ---------------------------------------------------------

# `python -m js.commit_helper` prints these with .text(): the commit agent reads
# its output through a shell and its survey goes into the agent's prompt, so no
# colour or slot is added.
COMMIT_HELPER_HELP = Message(
    "Usage: python3 -m js.commit_helper [-C DIR|--repo DIR] <command>\n"
    "\n"
    "  survey\n"
    "      Branch, porcelain status, staged and unstaged text diffs with every hunk\n"
    "      numbered per file, untracked files and recent log.\n"
    "  stage <file> <hunks|all>\n"
    "      Stage the named unstaged hunks of one tracked file, such as 1,3, or all of it.\n"
    "      An untracked file takes only `stage <file> all`.\n"
    "  commit <message-file> [--amend]\n"
    "      Commit the staged changes with the message read from a file.", banner=False)
COMMIT_HELPER_USAGE = Message("Usage: python3 -m js.commit_helper {usage}", banner=False)
GIT_NOT_A_REPO = Message("Not a git repository: {repo}", GRAVE)
GIT_FAILED = Message("Git failed in {repo}: git {argv}: {detail}", GRAVE)
GIT_EXIT = Message("exit {code}")
SURVEY_HEADING = Message("=== Survey: {repo} ===", banner=False)
SURVEY_BRANCH = Message("Branch: {branch}", banner=False)
SURVEY_DETACHED = Message("Detached HEAD at {sha}")
SURVEY_NO_COMMITS = Message("No commits yet")
SURVEY_STATUS = Message("-- Status --", banner=False)
SURVEY_STATUS_ROW = Message("{xy} {path}", banner=False)
SURVEY_CLEAN = Message("Clean tree. Nothing to commit.", banner=False)
SURVEY_STAGED = Message(
    "\n-- Staged diff. Already staged: review before commit. These hunk numbers do not address `stage`. --",
    banner=False)
SURVEY_UNSTAGED = Message(
    "\n-- Unstaged diff. Hunks are numbered per file: `stage <file> <n[,n]|all>` --", banner=False)
SURVEY_FILE = Message("\n### {path}: {hunks}", banner=False)
SURVEY_NO_TEXT_HUNKS = Message(
    "No text hunks. A binary, rename or mode change: stage the whole file.", banner=False)
SURVEY_HUNK = Message("  --- Hunk {index}: {header}", banner=False)
SURVEY_NONE = Message("None.", banner=False)
SURVEY_UNTRACKED = Message("\n-- Untracked --", banner=False)
SURVEY_UNTRACKED_ROW = Message("?? {path}  New file. `stage {path} all` adds it whole.", banner=False)
SURVEY_LOG = Message("\n-- Recent log --", banner=False)
SURVEY_NO_HISTORY = Message("No history.", banner=False)
STAGED_WHOLE = Message("Staged whole file: {path}")
STAGED_HUNKS = Message("Staged {path}: hunks {hunks} of {total}")
STAGE_NO_CHANGES = Message("{path} has no pending changes.", GRAVE)
STAGE_UNTRACKED = Message(
    "{path} is untracked. Use `stage {path} all`. Hunk specs work only for tracked text diffs.", GRAVE)
STAGE_BAD_SPEC = Message("Hunks must be comma-separated numbers or all. Got {spec!r}.", GRAVE)
STAGE_NO_HUNKS = Message(
    "No unstaged text hunks in {path}. A binary, rename or mode change, or already staged: "
    "use `stage {path} all`.", GRAVE)
STAGE_OUT_OF_RANGE = Message("Hunks {bad} out of range. {path} has {count}.", GRAVE)
STAGE_APPLY_FAILED = Message("{detail}\nFallback: `stage {path} all`", GRAVE)
GIT_APPLY_FAILED = Message("git apply failed")
MESSAGE_FILE_UNREADABLE = Message("Message file not read: {error}", GRAVE)
MESSAGE_FILE_EMPTY = Message("Commit message file is empty: {path}", GRAVE)
COMMITTED = Message("Committed.")
