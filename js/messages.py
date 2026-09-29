"""Every string js shows the operator, by name.

Code names an entry here and passes the values for its holes; the wording
lives only in this file. Strings the model reads (tool results, prompts, tool
descriptions) are not here.

A `Message` is in one register:

  banner   the client reporting its own state: the `BANNER` slot, a space,
           then the text. No template carries the slot literally.
  plain    content: tables, lists, headings, prompts the operator answers.

Severity is colour, never a word. `WARN` paints in light yellow and `GRAVE`
in light red, and only the holes: the values that tell the operator what
happened. A message without holes is painted whole.
"""

from __future__ import annotations

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

    def line(self, **fields: Any) -> str:
        """The message as the screen shows it."""
        body = _paint(self.template, fields, SEVERITY_COLOR[self.severity])
        return banner(body) if self.banner else body


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


def say(message: Message, /, *, file: Any = None, flush: bool = False, **fields: Any) -> None:
    """Print `message` to stdout, or to `file`."""
    print(message.line(**fields), file=file if file is not None else sys.stdout, flush=flush)


def warn(message: Message, /, **fields: Any) -> None:
    """Print `message` to stderr."""
    say(message, file=sys.stderr, flush=True, **fields)


def plural(n: int, word: str) -> str:
    """`n word`, with an s when n is not one."""
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------------------
# The entries. Grouped by where the operator meets them.
# ---------------------------------------------------------------------------

# Any failure whose text is already a whole sentence (an exception, a returned error).
FAILED = Message("{error}", GRAVE)
FAILED_WARN = Message("{error}", WARN)

# --- Session -----------------------------------------------------------------

RESUME_HINT = Message("Resume: {command}")

# --- Tool surface ------------------------------------------------------------

TOOL_SURFACE_KEPT = Message("{error}. Tool surface unchanged.", WARN)

# --- Compaction --------------------------------------------------------------

# compact_now's result. Callers tell a compaction from a skip by the
# `compacted:` start, so those words stay first.
COMPACTED = Message("compacted: kept tail from message {keep_from}/{total} using {model}")
COMPACT_SKIPPED_NO_PREFIX = Message("compact skipped: no new prefix to summarize")
COMPACT_SKIPPED_SAVINGS = Message("compact skipped: saves {savings} tokens, needs {required}")
COMPACTION_DONE = Message("{result}")
AUTO_COMPACT_ARMED = Message("Context {fullness:.0%} full. Auto-compaction armed.", WARN)
AUTO_COMPACT_PAUSED = Message(
    "Auto-compaction paused after two compactions in a row. It resumes when context drops below the trigger.", WARN)
SUMMARY_SPLIT = Message("Summary too large. Summarizing both halves, depth {depth}.", WARN)
CLEARING_FLIGHT_FAILED = Message("Tool-result clearing: flight log not opened: {error}", GRAVE)

# --- Model and provider ------------------------------------------------------

MODEL_SET = Message("Model: {model}")
DEFAULT_MODEL_SAVED = Message("Default model: {model}")
DEFAULT_MODEL_NOT_SAVED = Message("Model: {model}. Not saved as default: {error}", GRAVE)
MODELS_LIMIT_NOT_A_NUMBER = Message("/models: {value!r} is not a number.")
NO_PROVIDER = Message("No provider set. Use /provider <id> first.")
MODELS_NOT_LISTED = Message("Models not listed: {error}")
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
SAVE_FAILED = Message("Not saved: {error}")
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
LOAD_UNREADABLE = Message("load: {path} not read: {error}")
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
SKILL_IS_A_TURN = Message("skill <name> [request] is a turn. Type it at the input line.")
HELP_HEADING = Message("Commands:", banner=False)
HELP_ROW = Message("  {usage} {doc}", banner=False)
HELP_ALIAS = Message("alias: {body}")
HELP_ATTACH = Message("attach a file or image to that turn; quote paths with spaces")
HELP_EXIT = Message("quit")
TURNS_COUNT = Message("{messages} in context.")
SESSION_PATH = Message("{path}", banner=False)
NO_QUEUED_PROMPTS = Message("No queued prompts.")
ALIAS_TOO_DEEP = Message("alias {name}: nesting too deep.")
ALIAS_UNKNOWN_COMMAND = Message("alias {name}: unknown command {verb}")
TURN_RUNNING = Message("Turn running. {command} would clobber its context. ^C to cancel, or wait.")
UNKNOWN_COMMAND = Message("Unknown command: {verb}")

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
CD_NOT_A_DIR = Message("-C target is not a directory: {path}", GRAVE)
JSON_NEEDS_LIST = Message("--json requires --list.", GRAVE)
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
NOT_SAVED_NO_RESUME = Message("Session not saved. Resume unavailable.")

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

DEFAULT_VALUE = Message("default")
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

# --- Moving into ~/.js -------------------------------------------------------

HOME_MOVED = Message("Moved {source} to {target}")
HOME_WOULD_MOVE = Message("Would move {source} to {target}")
HOME_REMOVED_DUPLICATE = Message("Removed {source}: identical to {target}")
HOME_WOULD_REMOVE_DUPLICATE = Message("Would remove {source}: identical to {target}")
HOME_REMOVED_EMPTY = Message("Removed empty {source}")
HOME_WOULD_REMOVE_EMPTY = Message("Would remove empty {source}")
HOME_REFUSED = Message("Refused {source}: {reason}")
HOME_WOULD_REFUSE = Message("Would refuse {source}: {reason}")
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

# --- Session files -----------------------------------------------------------

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
EXPECTED_URL = Message("expected a URL starting with http:// or https://, got {value!r}")
EXPECTED_INTEGER = Message("expected an integer")
EXPECTED_LEVEL = Message("expected an integer from 0 to 3")
EXPECTED_POSITIVE_INTEGER = Message("expected an integer >= 1")
EXPECTED_NUMBER = Message("expected a number")
EXPECTED_POSITIVE_NUMBER = Message("expected a number > 0")
EXPECTED_JSON = Message("expected a JSON value")
EXPECTED_JSON_OBJECT = Message("expected a JSON object")
EXPECTED_ENV_NAMES = Message("expected a JSON list of non-empty environment-variable names")
UNKNOWN_PROVIDER_ID = Message("unknown provider id: {provider!r}. Pick a known id or add a custom one with js --login.")

# --- Status bar --------------------------------------------------------------

STATUS_COMPACTING = Message("compacting")
STATUS_CACHE = Message("cache {pct}%")

# --- Tool exchanges ----------------------------------------------------------

TOOL_ARGS_UNPARSED = Message("<arguments are not a JSON object>")
TOOL_MORE_LINES = Message("... +{count} lines")

# --- The kernel panel --------------------------------------------------------

KERNEL_MORE_LINES = Message("... {count} more lines. The model got the full text.")
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
