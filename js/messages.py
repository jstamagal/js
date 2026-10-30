"""Every string js shows the operator, by name.

Code names an entry here and passes the values for its holes; the wording
lives only in this file. Tool results, prompts and tool descriptions, which the
model reads, are not here. The commit helper's entries are: the operator runs
it, and the commit agent reads the same output through a shell.

An entry's name is also its /fset format's (`js.fset`): the screen shows a
message through that format, which by default is the entry's text with a `%s`
per hole.

A `Message` is in one register:

  banner   the client reporting its own state: `$BANNER`, the BANNER setting,
           a space, then the text. No template carries the slot literally.
  plain    content: tables, lists, headings, prompts the operator answers.

Severity is colour, never a word. `WARN` paints in COLOR_WARN and `GRAVE` in
COLOR_ERROR, and only the holes: the values that tell the operator what
happened. A message without holes is painted whole. `say` paints only a stream
that is a terminal.
"""

from __future__ import annotations

import argparse
import re
from contextlib import contextmanager
from contextvars import ContextVar
import sys
from dataclasses import dataclass, field, replace
from string import Formatter
from typing import Any

from . import fset
from .prompt_text import load_catalog

_PROMPTS = load_catalog('swarm')


# The banner slot: what starts every line the client says about its own
# state, and what $BANNER in a /fset format reads. The BANNER setting sets it
# (`set_banner`).
BANNER = "***"

@dataclass
class Banner:
    value: str = "***"


_BANNER: ContextVar[Banner | None] = ContextVar("js_message_banner", default=None)


@contextmanager
def using_banner(state: Banner):
    """Render and update this assembly's banner for the duration of the body."""
    token = _BANNER.set(state)
    try:
        yield state
    finally:
        _BANNER.reset(token)


def current_banner() -> str:
    state = _BANNER.get()
    return BANNER if state is None else state.value


INFO = "info"
WARN = "warn"
GRAVE = "grave"

# The colour entry each severity paints in; INFO paints nothing.
SEVERITY_ROLE = {INFO: "", WARN: "COLOR_WARN", GRAVE: "COLOR_ERROR"}

_FORMATTER = Formatter()


def _replace_key_labels(text: str, replacements: dict[str, str]) -> str:
    pattern = r"(?<![A-Za-z0-9/])(?:" + "|".join(re.escape(label) for label in sorted(replacements, key=len, reverse=True)) + r")(?![A-Za-z0-9/])"
    return re.sub(pattern, lambda match: replacements[match[0]], text)


@dataclass(frozen=True)
class Message:
    template: str
    severity: str = INFO
    banner: bool = True
    # The entry's name in this module, and the name of its /fset format.
    name: str = field(default="", compare=False)
    key_labels: tuple[tuple[str, str], ...] = field(default=(), compare=False, repr=False)

    def with_keymap(self, keymap: dict, defaults: dict) -> Message:
        """This hint with changed bindings named from its caller's keymap.

        The shipped labels stay as written while bindings match the defaults.
        Operator /fset formats retain their own wording.
        """
        from .keys import describe

        replacements = {}
        for label, actions in _HINT_ACTIONS.get(self.name, {}).items():
            if any(keymap.get(action, ()) != defaults.get(action, ()) for action in actions):
                sequences = dict.fromkeys(seq for action in actions for seq in keymap.get(action, ()))
                replacements[label] = "/".join(describe(seq) for seq in sequences) or KEY_UNBOUND
        if not replacements:
            return self
        escaped = {label: text.replace("{", "{{").replace("}", "}}") for label, text in replacements.items()}
        return replace(self, template=_replace_key_labels(self.template, escaped), key_labels=tuple(replacements.items()))

    def text(self, **fields: Any) -> str:
        """The message with its holes filled: no slot, no colour. For a
        channel that is not the screen, or that adds the slot itself."""
        return self.template.format(**fields)

    def default_format(self) -> str:
        """The /fset format this entry has when nothing sets one: `$BANNER`
        for a banner entry, then the template with a `%s` per hole."""
        parts = ["$BANNER "] if self.banner else []
        for literal, name, _spec, _conversion in _FORMATTER.parse(self.template):
            parts.append(fset.escape(literal))
            if name is not None:
                parts.append("%s")
        return "".join(parts)

    def format(self) -> str:
        """The entry's /fset format: the one set, else `default_format`."""
        if self.key_labels:
            labels = {label: fset.escape(text) for label, text in self.key_labels}
            found = fset.lookup(self.name, transform_default=lambda text: _replace_key_labels(text, labels)) if self.name else None
        else:
            found = fset.lookup(self.name) if self.name else None
        return self.default_format() if found is None else found

    def runs(self, *, whole: str = "", **fields: Any) -> list[fset.Run]:
        """The message through its /fset format as colour runs (`js.fset.runs`),
        severity the colour where the format sets none, else colour entry
        ``whole``. Every output target draws from these."""
        holes = _holes(self.template, fields)
        role = SEVERITY_ROLE[self.severity]
        return fset.runs(self.format(), holes, hole=role if holes else "", whole=whole or ("" if holes else role))

    def line(self, *, colour: bool = True, **fields: Any) -> str:
        """The message as the screen shows it, through its /fset format.
        `colour=False` is the plain text, for a channel that is not a screen."""
        parts = self.runs(**fields)
        return fset.to_ansi(parts) if colour else fset.to_plain(parts)

    def plain(self, **fields: Any) -> str:
        """The message through its /fset format as plain text, for a file or
        a dump that is not a screen."""
        return fset.to_plain(self.runs(**fields))

    def body_runs(self, **fields: Any) -> list[fset.Run]:
        """The entry's runs without the `$BANNER` its default format leads
        with: a piece of a row, or a line for a channel that adds the slot
        itself. A format set by the operator or a theme is drawn as written,
        a `$BANNER` it writes included."""
        fmt = self.format()
        if fmt == self.default_format() and fmt.startswith("$BANNER "):
            fmt = fmt[len("$BANNER "):]
        return fset.runs(fmt, _holes(self.template, fields))

    def words(self, **fields: Any) -> str:
        """The entry's wording through its /fset format, plain and without a
        leading `$BANNER`: for a channel that takes text and no colour, such
        as an argument parser's help or an input prompt."""
        return fset.to_plain(self.body_runs(**fields))

    def body(self, **fields: Any) -> str:
        """`body_runs` as text with 24-bit colour escapes (`js.fset.to_ansi`)."""
        return fset.to_ansi(self.body_runs(**fields))

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

    def words(self) -> str:
        """Its wording through its entry's format, plain (`Message.words`)."""
        return self.message.words(**self.fields)


def banner(text: str) -> str:
    """`text` behind the banner slot, the BANNER setting."""
    return f"{fset.banner_text()} {text}"


def set_banner(value: str) -> None:
    """Make ``value``, the BANNER setting, the banner slot."""
    global BANNER
    state = _BANNER.get()
    if state is None:
        BANNER = value
    else:
        state.value = value


def _holes(template: str, fields: dict[str, Any]) -> list[str | list[fset.Run]]:
    """The template's holes filled from ``fields``, in order, as text. A
    `said()` entry in a hole, or an error raised with one, is drawn through
    its own format: its colour runs, which keep their colours whatever the
    target (`fset.runs`)."""
    values: list[str | list[fset.Run]] = []
    for _literal, name, spec, conversion in _FORMATTER.parse(template):
        if name is None:
            continue
        value = _FORMATTER.get_field(name, (), fields)[0]
        if isinstance(value, BaseException) and len(value.args) == 1 and isinstance(value.args[0], Said):
            value = value.args[0]
        spec = _FORMATTER.vformat(spec or "", (), fields)
        if isinstance(value, Said) and conversion is None:
            values.append(_said_runs(value, spec))
            continue
        value = _FORMATTER.convert_field(value, conversion)
        values.append(format(value, spec))
    return values


def _said_runs(said: Said, spec: str) -> list[fset.Run] | str:
    """A `said()` entry's runs through its own format, padded as ``spec``
    pads it; its words alone when ``spec`` does more than pad."""
    parts = said.message.body_runs(**said.fields)
    words = fset.to_plain(parts)
    shaped = format(words, spec)
    at = shaped.find(words)
    if at < 0:
        return shaped
    return [("", shaped[:at]), *parts, ("", shaped[at + len(words):])]


def is_terminal(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def color_system(stream: Any) -> str | None:
    """The colour a renderer writing straight to ``stream`` draws in, as rich
    names it: 24-bit on a terminal with colour on, none anywhere else."""
    return "truecolor" if is_terminal(stream) and fset.colour_on() else None


def for_stream(text: str, stream: Any = None) -> str:
    """``text``, which may carry colour escapes, as ``stream`` (stdout by
    default) takes it: as it is on a terminal with colour on, plain anywhere
    else. Every printer that writes drawn text goes through here or
    `line_for`."""
    stream = stream if stream is not None else sys.stdout
    return text if is_terminal(stream) and fset.colour_on() else fset.plain(text)


def line_for(message: Message, /, *, file: Any = None, **fields: Any) -> str:
    """The line `say` prints to `file`, stdout by default."""
    stream = file if file is not None else sys.stdout
    return message.line(colour=is_terminal(stream), **fields)


def say(message: Message, /, *, file: Any = None, flush: bool = False, **fields: Any) -> None:
    """Print `message` to stdout, or to `file`."""
    stream = file if file is not None else sys.stdout
    print(line_for(message, file=stream, **fields), file=stream, flush=flush)


def words_of(text: str) -> str:
    """``text`` as plain text for a dump or a file: a `said()` entry worded
    through its format (`Said.words`), any other text as it is."""
    return text.words() if isinstance(text, Said) else text


def of_error(error: BaseException) -> str:
    """What ``error`` says: the `said()` entry it was raised with, which keeps
    its format for wherever it is shown, else its text."""
    if len(error.args) == 1 and isinstance(error.args[0], Said):
        return error.args[0]
    return str(error)


def failure_line(error: str) -> str:
    """A failure as the screen shows it: a `said()` entry through its own
    format, any other text through FAILED's."""
    if isinstance(error, Said):
        return line_for(error.message, **error.fields)
    return line_for(FAILED, error=error)


def say_said(said: Said, /, *, file: Any = None, flush: bool = False) -> None:
    """Print a message `said()` made."""
    say(said.message, file=file, flush=flush, **said.fields)


def warn(message: Message, /, **fields: Any) -> None:
    """Print `message` to stderr."""
    say(message, file=sys.stderr, flush=True, **fields)


class ArgumentParser(argparse.ArgumentParser):
    """argparse with its headings and its refusal in entries. A bad command
    line prints the usage, then ARGUMENTS_REFUSED on stderr, and exits 2."""

    def __init__(self, *args: Any, add_help: bool = True, on_error=None, **kwargs: Any) -> None:
        self.on_error = on_error
        kwargs.setdefault("formatter_class", _HelpFormatter)
        super().__init__(*args, add_help=False, **kwargs)
        self._positionals.title = ARGS_POSITIONALS.words()
        self._optionals.title = ARGS_OPTIONS.words()
        if add_help:
            self.add_argument("-h", "--help", action="help", help=OPT_HELP_THIS.words())

    def error(self, message: str) -> Any:
        if self.on_error is not None:
            return self.on_error(self, message)
        self.print_usage(sys.stderr)
        warn(ARGUMENTS_REFUSED, error=message)
        self.exit(2)


class _HelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage: Any, actions: Any, groups: Any, prefix: str | None = None) -> None:
        super().add_usage(usage, actions, groups, ARGS_USAGE_PREFIX.words() if prefix is None else prefix)


def plural(n: int, word: str) -> str:
    """`n word`, with an s when n is not one."""
    return (COUNT_ONE if n == 1 else COUNT_MANY).words(number=n, noun=word)


# ---------------------------------------------------------------------------
# The entries. Grouped by where the operator meets them.
# ---------------------------------------------------------------------------

# Any failure whose text is already a whole sentence (an exception, a returned error).
FAILED = Message("{error}", GRAVE)
# An error that is not js's own, named by its kind.
ERROR_OF_KIND = Message("{kind}: {error}", banner=False)
# A provider's failure: the HTTP status it stands for and the provider's message.
ERROR_OF_STATUS = Message("{status} {error}", banner=False)
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
AUTO_COMPACT_BREAKER = Message(
    "Auto-compaction paused after {failures} failed summaries in a row. /compact retries it.", WARN)
SUMMARY_SPLIT = Message("Summary too large. Summarizing both halves, depth {depth}.", WARN)
CLEARING_FLIGHT_FAILED = Message("Tool-result clearing: flight log not opened: {error}", GRAVE)

# --- Model and provider ------------------------------------------------------

MODEL_SET = Message("Model: {model}")
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
MIGRATE_DELETED = Message("{key}: setting deleted. Written as a comment; edit by hand.", WARN)
MIGRATE_WROTE = Message("Wrote {path} from {legacy}. Review it, then delete {legacy}. This flag is removed in 2 releases.")
SAVE_FAILED = Message("Not saved: {error}", GRAVE)
SAVED = Message("Saved {lines} to {path}")
SAVE_NOTHING = Message("Nothing changed this session. Nothing saved.")
SAVED_SERVER = Message("Saved {settings} as the {model} defaults of login {provider}")
SAVE_NO_SERVER = Message("Not saved: no saved login for {provider}. js --login adds one.", GRAVE)
NO_ALIASES = Message("No aliases.")
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
WIPE_ROTATED = Message("The conversation moved to session {path}")
WIPE_NOTHING = Message("No memory file to rotate.")
PERSONA_TRUNCATED = Message("Truncated. {size} bytes in all.")
BLOCKING_NO_JOBS = Message("{command} is unavailable under --blocking.")
NO_JOBS = Message("No running jobs.")
JOB_ROW = Message("[{id}] {kind}{label}", banner=False)
NO_JOB_TO_CANCEL = Message("No matching job to cancel.")
CANCELLING = Message("Cancelling {jobs}", WARN)
NO_SUCH_SKILL = Message("No skill named {name!r}.")
SKILL_IS_A_TURN = Message("skill <name> [request] is a turn. Type it at the input line.")
HELP_HEADING = Message("Commands:", banner=False)
HELP_ROW = Message("  {usage} {doc}", banner=False)
HELP_SETTING = Message("{key}", banner=False)
HELP_SETTING_LINE = Message("    {text}", banner=False)
HELP_ALIAS = Message("Alias: {body}")
HELP_PROMPT_COMMAND = Message("Command file {path}: {description}")
HELP_ATTACH = Message("Attach a file or image to that turn. Quote a path with spaces.")
HELP_EXIT = Message("Quit.")
TURNS_COUNT = Message("{messages} in context.")
COST_NONE = Message("No model calls in this session yet.")
COPY_DONE = Message("Copied answer {n} ({chars} characters) to the clipboard.")
COPY_NOTHING = Message("No answer {n} back to copy.")
COPY_USAGE = Message("Usage: /copy [N] — N answers back, 1 the last.")
COPY_NO_CLIPBOARD = Message("No clipboard: set COPY_COMMAND, or run under Wayland (wl-copy) or X11 (xclip).")
COST_TOTAL = Message("Session: {calls}, {cost}.")
COST_TOKENS = Message(
    "  input {input}: cache read {cache_read}, cache write {cache_write}. "
    "Output {output}: reasoning {reasoning}.", banner=False)
COST_MODEL = Message("  {model}: {calls}, {tokens} tokens, {cost}", banner=False)
COST_UNPRICED = Message("no price")
COST_PARTLY_PRICED = Message("{cost} + {calls} with no price")
SESSION_TITLED = Message("Session titled: {title}")
SESSION_TITLE = Message("{title}")
SESSION_UNTITLED = Message("This session has no title. /name <text> gives it one.")
SESSION_NOT_SAVED_TITLE = Message("This session is not saved. Nothing to title.", WARN)
CWD_IS = Message("{path}")
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
KEY_UNBOUND = "unbound"
_HINT_ACTIONS = {
    "INSTALL_ASK_PROVIDER": {"a": ("add",)},
    "TURN_RUNNING": {"^C": ("interrupt",)},
    "PRESS_CTRL_C_AGAIN": {"^C": ("interrupt",)},
    "STARTUP_HINT": {"Ctrl-D": ("eof",)},
    "PICKER_KEYS": {"↑↓/jk": ("up", "down"), "pgup/pgdn": ("page_up", "page_down"),
                    "/": ("search",), "enter": ("choose",), "q/esc": ("quit",)},
    "PICKER_KEYS_CHECKLIST": {"↑↓/jk": ("up", "down"), "/": ("search",), "space": ("toggle",),
                              "a": ("all",), "n": ("none",), "enter": ("choose",), "q": ("quit",)},
    "PICKER_KEYS_CHECKLIST_EDIT": {"↑↓/jk": ("up", "down"), "/": ("search",), "space": ("toggle",),
                                   "a": ("all",), "n": ("none",), "e": ("edit",),
                                   "enter": ("choose",), "q": ("quit",)},
    "PICKER_KEYS_SEARCH": {"enter": ("choose",), "esc": ("back",)},
    "PICK_HELP": {"tab": ("focus",), "↑↓": ("up", "down"), "enter": ("choose",),
                  "f": ("fetch",), "esc/q": ("quit",)},
    "SKILLS_KEYS": {"space": ("toggle",), "enter": ("choose",), "/": ("search",), "esc": ("back",)},
    "SKILLS_QUERY_KEYS": {"enter": ("choose",), "esc": ("back",)},
    "SESSIONS_KEYS": {"v": ("view",), "/": ("search",), "b": ("messages",),
                      "i": ("info",), "a": ("all",), "esc": ("back",)},
    "SESSIONS_QUERY_KEYS": {"enter": ("choose",), "esc": ("back",)},
    "SESSIONS_MESSAGES_KEYS": {"enter": ("choose",), "r": ("resume",), "esc": ("back",)},
    "SESSIONS_INFO_KEYS": {"enter": ("choose",), "esc": ("back",)},
}

TURN_RUNNING = Message("Turn running. {command} would clobber its context. ^C to cancel, or wait.")
UNKNOWN_COMMAND = Message("Unknown command: {verb}")
UNKNOWN_SLASH_COMMAND = Message("Invalid command", WARN)

# --- /help: what each command does -------------------------------------------

CMD_HELP = Message("Show this message. help set NAME explains a setting.")
CMD_SET = Message("List settings, a group or a *_FILE wildcard, show one, or change one. set -NAME clears one.")
CMD_FSET = Message("List what js looks like, show one entry, or change one. fset -NAME clears one.")
CMD_SHOW = Message("List every setting and its effective value.")
CMD_SAVE = Message("Write what you changed this session (settings, model, /fset, handlers, aliases) into the global jsrc, in place. /save server writes the model settings into the saved login.")
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
CMD_WIPE = Message("Move the conversation to a new session file and clear it here.")
CMD_PERSONA = Message("Print the system prompt.")
CMD_TOOLS = Message("Show each tool's state and the entry that decided it. A state is eager, lazy or ban.")
CMD_SKILL = Message(
    "List skills. With a name, send that skill's instructions with the request. User-only skills work too.")
CMD_SKILLS = Message("Open the skill picker: switch each skill on or off. Enter sets SKILLS_DISABLED.")
CMD_TURNS = Message("Count the messages in context.")
CMD_COST = Message("Show the session's tokens and cost, in total and by model.")
CMD_COPY = Message("Copy the last answer, or the one N back, to the clipboard as plain text.")
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
CMD_EXEC = Message(
    "Run a shell command; its stdout goes to the model with the next message. In an "
    "`on` handler it reads the event as JSON on stdin, and exit status 2 refuses a tool_call.")

# --- /exec -------------------------------------------------------------------
EXEC_QUEUED = Message("exec: {lines} queued for the next message.")
EXEC_NO_OUTPUT = Message("exec: no output.")
EXEC_EXITED = Message("exec: {command} exited {status}: {detail}", WARN)
EXEC_TIMED_OUT = Message("exec: {command} killed after {seconds}s.", WARN)
EXEC_FAILED = Message("exec: {command} did not start: {error}", WARN)

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
    "  -i          Incognito: keep your shell history out of the prompt.\n"
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
    "  --extra K=V One-off config, e.g. --extra SUBAGENT_DEPTH_MAX=3\n"
    "\n"
    "MORE\n"
    "  js --login [PROVIDER]   Sign in.\n"
    "  js --list-models        List the runnable models.\n"
    "  js --commit             Run the commit agent.\n"
    "  js -p 'task' --loop 10   Repeat fresh turns; 45M and 3H loop by time.\n"
    "  js --help-full          Every option.\n",
    banner=False)

# argparse: its headings, its refusal, and each option's help.
ARGS_USAGE_PREFIX = Message("Usage: ")
ARGS_POSITIONALS = Message("Arguments")
ARGS_OPTIONS = Message("Options")
ARGUMENTS_REFUSED = Message("{error}", GRAVE)
UNKNOWN_OPTION = Message("unknown option {option}", GRAVE)
UNKNOWN_OPTION_DID_YOU_MEAN = Message("unknown option {option} (did you mean {suggestion}?)", GRAVE)
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
    "Desugars to --extra MODEL/PROVIDER/BASE_URL/API_KEY, "
    "so an explicit --extra still wins.")
OPT_CD = Message(
    "Keep the agent in DIR, for every mode: -p, the REPL, --commit and the rest. DIR is the "
    "working directory. Every tool that starts a process runs under bubblewrap: DIR "
    "read-write, the system read-only, /home hidden except PATH directories and JAIL_BIND "
    "entries, a private /tmp, the network on. File tools refuse paths outside DIR and the "
    "bound paths. Needs bwrap.")
OPT_DEBUG = Message(
    "In prompt and --bench modes, stream the concise per-turn diagnostics and the answer live to the terminal: "
    "run header, tool-call lines, per-call timing. The full request trace still goes only to the debug "
    "autolog file.")
OPT_DEBUG_FILE = Message(
    "Also write the full byte-honest request trace to PATH: unclipped system prompt, full tool-schema JSON "
    "with descriptions, the messages sent each call, and per-call timings. The clean final answer still "
    "prints to stdout. REQUEST_LOG always writes the same trace under logs/<agent>/<session>.log.")
OPT_SESSION = Message(
    "--session NAME: resume NAME, looking in this directory's sessions first, then in all of them."
    " Not found: start it. Bare, open the session picker.")
OPT_SESSION_KEY = Message("Derive a stable session name from agent, cwd and caller key.")
OPT_NO_SAVE = Message(
    "Expensive throwaway prompt or pipe run. Nothing is saved, resume is unavailable, "
    "and the next run must re-read context.")
OPT_QUIET = Message("Suppress the resume hint after a one-shot prompt.")
OPT_REASONING = Message(
    "Thinking effort: off|minimal|low|medium|high|xhigh|max. off disables thinking. Any other value is rejected.")
OPT_MAX_OUT = Message("Max output tokens per call.")
OPT_SYSTEM_PROMPT_FILE = Message("Use one file as the full system prompt, with inline directives.")
OPT_SYSTEM_PROMPT = Message("Use TEXT as the full system prompt.")
OPT_YOU_ARE = Message("Identify the selected model with a minimal system prompt.")
OPT_APPEND_SYSTEM_PROMPT = Message("Append TEXT to the selected system prompt.")
OPT_NOENV = Message("Suppress envctx output.")
OPT_CHAT = Message("Omit agent and instruction files, envctx, and tools. Explicit tool selections still apply.")
OPT_WITH_TOOLS = Message("Select only these comma-separated tools, optionally :eager or :lazy.")
OPT_WITHOUT_TOOLS = Message("Exclude these comma-separated tool names or globs.")
OPT_TOOLS = Message("Use a JSON list of tool-policy selectors.")
OPT_NO_AGENTS_MD = Message("Do not load JS.md or AGENTS.md instruction files.")
OPT_NO_SKILLS = Message("Disable skill discovery and the skill tool.")
OPT_NO_MCP = Message("Disable MCP servers.")
OPT_BARE = Message("Use slim without envctx, skills, or MCP.")
OPT_VERIFY = Message("Validate configuration, agents, and reachable skills without executing prompts.")
OPT_BENCH_MODELS = Message("Benchmark each saved model with Article I, sections 1–5.")
OPT_REMOVE_IF_FAIL = Message("Remove saved models on slow_decode[:TPS],slow_ttft[:SECONDS],failure.")
OPT_BENCH = Message(
    "Benchmark mode: run AGENT's NN-benchmark.md turns each on a clean slate without a session, "
    "measuring TTFT, tok/s and turn time. Pair with --stats-json or --stats-csv.")
OPT_LOCK = Message("Refuse to start while another --lock run of the same agent is running. Works with --bench, --agent, or a bare run.")
OPT_LOOP = Message(
    "Repeat fresh invocations: a count, minutes as 45M, hours as 3H, or bare until Ctrl-C. "
    "The deadline is checked between invocations; all other options carry through.")
OPT_STATS_JSON = Message("Write per-turn stats to PATH as JSON: ttft, tok/s, turn time, tokens.")
OPT_STATS_CSV = Message("Write per-turn stats to PATH as CSV.")
OPT_BLOCKING = Message(
    "Run the legacy blocking REPL: input waits for the turn to finish, and ^C exits. The default runs one "
    "async event loop, so input stays live while a turn streams and subagents run, and ^C cancels the "
    "active turn.")
OPT_EXTRA = Message(
    "Set a dotted config key for this run, e.g. --extra SUBAGENT_DEPTH_MAX=3. May be repeated. "
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
OPT_SWARM = Message("With -p: be agent NAME on the swarm bus at ROOT. Adds the bus tools, "
                    "delivers inbox messages at every tool boundary, and sleeps on the inbox instead of "
                    "exiting when a turn ends. A message of kind stop ends it.")
SWARM_CLI = Message("Post on a swarm bus, list who is on it, ask whether it is quiet, see or post work, "
                    "or run a spec of agents in one process.")
SWARM_SEND = Message("Post a message. A text of - or no text reads stdin.")
SWARM_SEND_TO = Message("An agent's name, or * for everyone else on the bus.")
SWARM_SEND_KIND = Message("say, ask, reply, done, or stop. A stop ends the agent after its current turn.")
SWARM_MEMBERS = Message("Who is on the bus, asleep or working, and what each holds.")
SWARM_QUIET = Message("Exit 0 when every agent on the bus is asleep with an empty inbox, else 1.")
SWARM_CELLS = Message("The work board: open and taken tasks first, done ones last.")
SWARM_POST = Message("Put a task on the work board. A body of - reads stdin.")
SWARM_RUN = Message("Run every agent in a JSON spec in this one process; events go to stdout.")
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
    "stay as written instead of running. Inline code runs by default. Set INLINE_CODE off or "
    "JS_INLINE_CODE=0 to make that permanent. {{{{VAR}}}} env expansion and !{{env}}/!{{file}} are "
    "always on.")
OPT_NO_SHELL_HISTORY = Message(
    "Keep your recent shell commands out of the prompt for this run: envctx skips its recent_shell block. "
    "Set ENVCTX_HIST=0 in your environment to make that permanent.")
OPT_PRINTONLY = Message(
    "Dry run: assemble what would be sent and print it instead of calling the model, then exit. "
    "LETTERS pick sections: t=tools p=prompt e=env-expanded i=inlines-expanded b=benchmark "
    "x=startup exchanges a=everything. "
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
NO_ANSWER = Message("The model returned no answer.", GRAVE)
CONTINUE_HINT = Message("Continue: {command}")

# One-shot: what a turn is doing, while nothing else shows it.

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

NO_SKILLS = Message("No skills found.")
SKILL_ROW = Message("{name} {source} {description}", banner=False)
SKILL_ROW_USER_ONLY = Message("{name} user-only {source} {description}", banner=False)
SKILL_HINT = Message("/skill <name> [request] sends one with your request.")
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
PRINTONLY_NO_EXCHANGES = Message("No NN-user.md / NN-agent.md files.")
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
CLIPBOARD_NONE = Message("No clipboard: no Wayland or X11 display. PASTE_IMAGE_COMMAND names a command that prints the image.", WARN)
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

# Each a line of the REPL's opening lines; js/fset gives them their colours.
STARTUP_TITLE = Message("me — js agent", banner=False)
STARTUP_AGENT = Message("Agent:  {agent}", banner=False)
STARTUP_MODEL = Message("Model:  {model}", banner=False)
STARTUP_PROMPT = Message("Prompt: {prompt}", banner=False)
STARTUP_MEMORY = Message("Memory: {memory}", banner=False)
STARTUP_HINT = Message("Type exit or Ctrl-D to quit. /help lists commands.", banner=False)
RESUMED_MODEL = Message("Model: {model}")
RESUME_MODEL_FALLBACK = Message("No login for {stamped}, the model this session last ran on. Continuing on {model}.", WARN)
RESUMED = Message("Resumed: {messages}.")
EMPTY_SESSION = Message("Empty session. Nothing to resume: {path}", WARN)
PROMPT_CHANGED = Message("Agent prompt changed on disk. Session keeps the one it started with.")
NO_SUCH_AGENT = Message(
    "No such agent: {agent}. Looked in project .js/agents, {agents_dir}, and repo prompts. "
    "Create {new_dir}/ with NN-*.md prompt files and an optional agent.yaml manifest.", GRAVE)

# --- A turn ------------------------------------------------------------------

# The turn's own report lines. `label` is "" or the caller's, "Subagent 2: ".
RUN_LINE = Message("run  {fields}")
CALL_STATS = Message("{label}{ms}ms  finish={finish}  tool_calls={tool_calls}  {tokens} tok  {tps:.1f} tok/s{ttft}{cache}")
RETRY_BUDGET_EXHAUSTED = Message("Tool-loop retry budget exhausted.", GRAVE)
MAX_ITERATIONS = Message("Tool loop hit max iterations: {limit}", GRAVE)
RESPONSE_INCOMPLETE = Message("Response incomplete: {reason}", WARN)
MAX_OUTPUT_ESCALATED = Message("Reply hit its output cap of {before}; sending again with {after}.")
MAX_OUTPUT_RESUMING = Message("Reply hit its output cap; asking the model to resume, {n} of {limit}.", WARN)
RETRY_WAIT = Message("Retry {n}/{of} in {seconds:.1f}s: {failure}")
RETRY_GAVE_UP = Message("Gave up after {n} retries.", GRAVE)
RETRY_COUNTDOWN = Message("Retry {n}/{of} in {seconds:.0f}s")
STEERED = Message("Steered.")
FLIGHT_LOG_FAILED = Message("Flight log not written: {error}", GRAVE)
COMPACTION_FAILED = Message("Compaction failed: {error}", GRAVE)

# --- The network channel -----------------------------------------------------

# A network line built from pieces, and a line behind its caller's label.
NET_LINE = Message("{text}")
NET_LABELLED = Message("{label}: {text}")
NET_CONNECTING = Message("Connecting: {url}")
NET_CONNECTED = Message("Connected: {host}  {ms}ms")
NET_ROLE_CONNECTING = Message("{role}: connecting {url}")
NET_ROLE_CONNECTING_AGENT = Message("{role}: connecting {url}  agent={agent}")
NET_ROLE_CONNECTED = Message("{role}: connected: {host}  {ms}ms")
NET_COMPACTING = Message("Compacting: {model} via {url}")
NET_CACHE_BREAK = Message(
    "Prompt cache break: cache read {before} -> {after} tokens, down {drop:.0%}, {idle}s since the last request")
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
PICKER_KEYS_CHECKLIST_EDIT = Message("↑↓/jk move  / search  space toggle  a all  n none  e defaults  enter save  q back")
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
LOGIN_PLACEHOLDER_KEY = Message("No key given. Storing placeholder '{key}'; local endpoints ignore it.")
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
LOGIN_PROVIDER_KEYS = Message("↑↓/jk move  / search  enter select  r rename  d delete  q/esc back")
LOGIN_TYPEAHEAD_KEYS = Message("Type to filter  ↑↓ move  backspace erase  enter select  esc back")
LOGIN_RENAME = Message("New provider id")
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
LOGIN_MODELS_UNREACHABLE = Message("Could not list models: {error}. Saving the server anyway.", WARN)
LOGIN_DEFAULTS = Message("Defaults")
LOGIN_DETAIL_DEFAULTS = Message("Defaults: {defaults}")
LOGIN_EDIT_DEFAULTS = Message("Defaults NAME=value,NAME=value. NAME is one of {names}. Enter keeps saved, - clears")
LOGIN_EDIT_MODEL_DEFAULTS = Message("{model} defaults NAME=value,NAME=value, - clears: ")
LOGIN_BAD_DEFAULTS = Message("{error}. Nothing saved yet.", WARN)
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

# --- just install ------------------------------------------------------------
INSTALL_ASK_KEY = Message("{name} for {use}. Enter skips")
INSTALL_KEY_SAVED = Message("Saved {name} to {path}")
INSTALL_KEYS_MISSING = Message("Not set: {names}. Run just install in a terminal to save them to {path}", WARN)
INSTALL_MODEL_UNROUTED = Message("Default model {model} has no provider to run on", WARN)
INSTALL_MODEL_UNSET = Message("No default model is set", WARN)
INSTALL_LOGINS = Message("Saved logins: {logins}")
INSTALL_LOGIN_ROW = Message("{provider} with {models}", banner=False)
INSTALL_ASK_PROVIDER = Message("a adds a provider, Enter skips")

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
HOME_NOTHING_TO_MOVE = Message("Nothing to move into {home}.")
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

SESSION_AMBIGUOUS = Message("Session {session} is in more than one directory: {paths}")
SESSION_NOT_RESERVED = Message("No free session name in {directory}")
SESSION_BRANCH_NO_MESSAGE = Message("{path} has no message {message}")
SESSION_RECORDS_SKIPPED = Message(
    "{path}: skipped {records} from an incompatible schema version. No migration to {version} yet. "
    "History may be incomplete.", WARN)

# --- The ex line -------------------------------------------------------------

BUFFER_WRITTEN = Message("Buffer written to {path}. Not sent.")
EX_UNKNOWN = Message("Not an editor command, js command or program: {verb}", GRAVE)
EX_FAILED = Message(":{verb}: {error}", GRAVE)

# --- The keys file -----------------------------------------------------------

KEYS_BAD_KEY = Message("not a key name: {key!r}. Key names are like c-r, escape, enter, pageup, f5, x")
KEYS_BIND_USAGE = Message("bind needs a key and an action: bind KEY... ACTION")
KEYS_UNBIND_USAGE = Message("unbind needs a key: unbind KEY...")
KEYS_UNKNOWN_ACTION = Message("unknown action: {action}")
KEYS_UNKNOWN_VERB = Message("unknown command {verb!r}: expected bind or unbind")
KEYS_UNREADABLE = Message("{path} not read: {error}")
# Under the input line after the interrupt or eof key arms the exit.
EXIT_ARMED = Message("Press {key} again to exit", banner=False)

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
    "--im-a-pussy, set INLINE_CODE off or JS_INLINE_CODE=0")
DIRECTIVE_ENV_NEEDS_NAME = Message("!{{env ...}} needs a variable name. Got {body!r}")
DIRECTIVE_NOT_ON_PATH = Message("{label}: {program!r} not found on PATH")
DIRECTIVE_TIMED_OUT = Message("{label}: timed out after {seconds}s")
DIRECTIVE_EXITED = Message("{label}: exited {code}: {stderr}")
DIRECTIVE_EXITED_SILENT = Message("{label}: exited {code}. No stderr")
DIRECTIVE_EXITED_OUTPUT_USED = Message("{label}: exited {code}. Its output was used", WARN)
FLIGHT_AUTOLOG_FAILED = Message("Flight autolog not written: {error}. Flight {path}", GRAVE)
FLIGHT_NOTICE = Message("Compaction {stage} {id}: operation={operation} {detail} flight={path}")

# --- Settings verbs ----------------------------------------------------------

UNKNOWN_SETTING = Message("Unknown setting: {key}")
EXTRA_UNKNOWN_SETTING = Message("--extra: unknown setting: {key}")
SETTING_IS = Message("The value of {key} is: {value}")
SETTING_CHANGED = Message("Value of {key} changed to: {value}")
SETTING_ROW = Message("{key} {value}")
SETTING_LIVE = Message("{line}  live: {source}", banner=False)
SETTING_IS_FSET = Message("{key} is how js looks: /fset {key}")
NO_EVENT_HANDLERS = Message("No event handlers.")
ON_NEEDS_TWO = Message("on needs an event and a handler.")
SET_NEEDS_TWO = Message("set needs a key and a value: {line!r}")
EXPECTED_ONE_OF = Message("expected {choices}")
EXPECTED_COLOUR = Message("expected a #rrggbb colour, got {value!r}")
EXPECTED_COLOUR_VALUE = Message("expected #rrggbb, a palette slot 00-98 or $NAME, several joined by |, got {value!r}")
EXPECTED_KEY = Message("expected prompt_toolkit key names such as c-v or escape v, got {value!r}")
EXPECTED_URL = Message("expected a URL starting with http:// or https://, got {value!r}")
EXPECTED_INTEGER = Message("expected an integer")
EXPECTED_LEVEL = Message("expected an integer from 0 to 3")
EXPECTED_POSITIVE_INTEGER = Message("expected an integer >= 1")
EXPECTED_NONNEGATIVE_INTEGER = Message("expected an integer >= 0")
EXPECTED_NUMBER = Message("expected a number")
EXPECTED_NONNEGATIVE_NUMBER = Message("expected a number >= 0")
EXPECTED_POSITIVE_NUMBER = Message("expected a number > 0")
EXPECTED_FINITE_NUMBER = Message("expected a finite number")
EXPECTED_RANGE = Message("expected a number from {low} to {high}")
EXPECTED_AT_LEAST = Message("expected a number >= {low}")
EXPECTED_JSON = Message("expected a JSON value")
EXPECTED_JSON_OBJECT = Message("expected a JSON object")
EXPECTED_ENV_NAMES = Message("expected a JSON list of non-empty environment-variable names")
EXPECTED_SKILL_NAMES = Message("expected a JSON list of non-empty skill names")
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
    "Model {model!r} has no provider and no login. Set PROVIDER or JS_PROVIDER, run `js --login`, "
    "or prefix a logged-in provider. `js --list-models` lists what is runnable.")
PROVIDER_NEEDS_KEY = Message(
    "Provider {provider!r} needs an API key. Run `js --login {provider}` or `set API_KEY <value>`.")
PROVIDER_NO_AUTH_METHOD = Message(
    "Provider {provider!r} needs an API key: the SDK found no way to authenticate. "
    "Run `js --login {provider}` or `set API_KEY <value>`.")
PROVIDER_UNKNOWN = Message(
    "Unknown provider {provider!r}. Run `js --login {provider}`. `js --list-models` lists what is runnable.")
PROVIDER_AUTH_FAILED = Message(
    "Provider {provider!r} authentication failed: {detail}. "
    "Run `js --login {provider}` or `set API_KEY <value>`.")
PROVIDER_NOT_CONFIGURED = Message(
    "Provider {provider!r} is not configured: {detail}. "
    "Run `js --login {provider}` or `set API_KEY <value>`.")

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

# Each field of the status bar, drawn only when it has a value.
STATUS_CLOCK = Message("[{clock}]", banner=False)
STATUS_ROUTE = Message("{route}", banner=False)
STATUS_CONTEXT = Message("ctx {tokens}", banner=False)
STATUS_SPINNER = Message("{spinner}", banner=False)
STATUS_PHASE = Message("{phase}", banner=False)
STATUS_REPLY = Message("out {tokens}", banner=False)
STATUS_WHO = Message("{who}", banner=False)
STATUS_CACHE = Message("cache {pct}%", banner=False)
STATUS_COST = Message("{cost}", banner=False)
STATUS_COMPACTING = Message("compacting", banner=False)

# --- Reasoning ---------------------------------------------------------------

REASONING_HEADING = Message("reasoning", banner=False)
REASONING_COUNT = Message("{tokens} tok", banner=False)
REASONING_EXPAND = Message("  {key} to expand", banner=False)

# --- Tool exchanges ----------------------------------------------------------

TOOL_ARGS_UNPARSED = Message("<arguments are not a JSON object>")
TOOL_MORE_LINES = Message("… {count} lines hidden")
TOOL_TRIES_LEFT = Message("{left} of {allowed} tries left")
TOOL_NO_OUTPUT = Message("no output")
TOOL_RUNNING = Message("running {elapsed}")
# Before a call's last word on the row under its header.
TOOL_RESULT_LEAD = Message("⎿")
# After a folded tool row on the screen: the key that unfolds it.
TOOL_EXPAND = Message("{key} to expand", banner=False)

# --- The kernel panel --------------------------------------------------------

KERNEL_MORE_LINES = Message("... {count} more lines. The model got the full text.")
KERNEL_MORE_CODE_LINES = Message("... {count} more lines of code.")
KERNEL_EVENT_MORE_LINES = Message("... {count} more lines.")
KERNEL_CELL = Message("Kernel cell {cell}:")
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

# --- The skill picker ----------------------------------------------------------

SKILLS_TITLE = Message(" SKILLS  {on} of {total} on", banner=False)
SKILLS_KEYS = Message("space=on/off  enter=keep  /=search  esc=close", banner=False)
SKILLS_QUERY_KEYS = Message("enter=keep  esc=clear", banner=False)
SKILLS_NONE = Message("No skills match.", banner=False)
SKILLS_ON = Message("[x]", banner=False)
SKILLS_OFF = Message("[ ]", banner=False)

# --- The session picker --------------------------------------------------------

SESSIONS_TITLE = Message(" SESSIONS  {count} · {view} · {order} · {scope}", banner=False)
SESSIONS_KEYS = Message("v=view  /=search  b=messages  i=info  a=all  esc=close", banner=False)
SESSIONS_VIEW_FLAT = Message("flat", banner=False)
SESSIONS_VIEW_DIR = Message("by dir", banner=False)
SESSIONS_VIEW_AGENT = Message("by agent", banner=False)
SESSIONS_NEWEST = Message("last used first", banner=False)
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

# --- /fset ---------------------------------------------------------------------

FSET_UNKNOWN = Message("Unknown /fset entry: {name}")
# A refusal of a value for NAME, the reason worded by its own format.
FSET_REFUSED = Message("{name}: {error}")
FSET_NO_CODE_STYLE = Message("no Pygments style {value!r}")
FSET_MARK_MODIFIER = Message("a mark takes no variation selector or skin-tone modifier")
FSET_VALUE = Message("{name} {value}")
FSET_EMPTY = Message("{name}")


COMPACTING_ROLE = Message("Compacting", banner=False)

CLEARING_OVERFLOW_START = Message("provider overflow; round={round_}; keeping newest {keep_recent} tool results", banner=False)

CLEARING_OVERFLOW_RESULT = Message("cleared {cleared} old tool results; reclaimed {reclaimed} chars", banner=False)

CLEARING_NO_RESULTS = Message("no eligible tool results; summary recovery may follow", banner=False)

CLEARING_BUDGET_START = Message("{value} context budget; keeping newest {keep_recent} tool results", banner=False)

CLEARING_BUDGET_RESULT = Message("cleared {total} old tool results; reclaimed {reclaimed} chars", banner=False)

CLEARING_STILL_OVER_BUDGET = Message("; still over budget", banner=False)

SUMMARY_RESPONSE_INCOMPLETE = Message("summary response incomplete: {value}", banner=False)

SUMMARY_RESPONSE_NO_TEXT = Message("summary response did not contain a completed text summary", banner=False)

COMPACTION_FLIGHT_START = Message("model={compact_model} trigger={value} messages={original_len}", banner=False)

STREAM_IDLE_NO_BYTES = "no response bytes for {seconds:g}s (STREAM_IDLE_TIMEOUT)"

RESPONSES_STREAM_FAILED = Message("OpenAI Responses stream failed", banner=False)

SAMPLING_SAMPLING_MUST_BE_A_NUMBER = Message("sampling.{key} must be a number", banner=False)

SAMPLING_SAMPLING_MUST_BE_A_FINITE_NUMBER = Message("sampling.{key} must be a finite number", banner=False)

SAMPLING_SAMPLING_MUST_BE_AN_INTEGER = Message("sampling.{key} must be an integer", banner=False)

EVENT_HANDLER_COMMAND_UNSUPPORTED = Message("unsupported event handler command: {value}", banner=False)

SETTING_SOURCE_LOGIN = Message("login {provider_id}", banner=False)

SETTING_SOURCE_SESSION = Message("session", banner=False)

SETTING_SOURCE_PROVIDER_DEFAULT = Message("provider default", banner=False)

SETTING_SOURCE_ENVIRONMENT_VARIABLE = Message("env {env_var}", banner=False)

SETTING_SOURCE_ENVIRONMENT = Message("env", banner=False)

SETTING_SOURCE_AGENT_SAMPLING = Message("agent sampling", banner=False)

SETTING_SOURCE_LOGIN_LAYER = Message("login {provider_id} {source}", banner=False)

SETTING_SOURCE_UNSET = Message("unset", banner=False)

LOOP_LOOP_EXPECTS_A_POSITIVE_TURN_COUNT_MINUTES = "--loop expects a positive turn count, minutes (M), or hours (H)"

LOOP_ATTEMPTS_EXHAUSTED = Message("STOP: {completed} completed turns after {attempts} attempts.", banner=False)

LOOP_UNLIMITED = "unlimited"

LOOP_ATTEMPT = Message("\n=== js loop attempt {attempts}, completed {completed}/{value} ===", banner=False)

LOOP_CHILD_STOPPED = Message("STOP: child exited {code}; {completed} turns completed.", banner=False)

LOOP_CHILD_RETRY = "Child exited {code}; retrying in {delay}s ({failures}/{failure_limit} consecutive failures)."

LOOP_STOPPED = Message("Stopped by operator; owned child has exited.", banner=False)

DIRECTIVE_FENCED_DIRECTIVE_BODY_CARRIES_A_LINE_THE = Message("fenced directive body carries a ``` line; the closing fence is ambiguous, so the directive is not run - indent the nested fence four spaces or split the directives", banner=False)

DIRECTIVE_ = Message("!{{{name}}}: {__name__}: {exc}", banner=False)

DIRECTIVE_UNREADABLE_AS_TEXT = "unreadable as text: {path} ({exc})"

SETTING_VALUE_OFF = Message("off", banner=False)

SETTING_VALUE_ON = Message("on", banner=False)

SETTING_VALUE_UNSET = Message("<unset>", banner=False)

SETTING_VALUE_NONE = Message("<none>", banner=False)

SETTING_VALUE_SECRET = Message("<set>", banner=False)

EVENT_HANDLER_REGISTERED = Message("on {prefix}{event} = {handler}", banner=False)

TOOL_SIZE_BYTES = Message("{count} B", banner=False)

TOOL_SIZE_KILOBYTES = Message("{value:.1f} KB", banner=False)

TOOL_SIZE_MEGABYTES = Message("{value:.1f} MB", banner=False)

COUNT_ONE = Message("{number} {noun}", banner=False)

COUNT_MANY = Message("{number} {noun}s", banner=False)

TOOL_EXIT_CODE = Message("exit {exit_code}", banner=False)

TOOL_LINES_OF_TOTAL = Message("{total} of {value}", banner=False)

TOOL_LINES_SHOWN = Message("{shown} shown", banner=False)

TOOL_BYTE_RANGE = Message("bytes {value}-{value2}", banner=False)

TOOL_LINE_RANGE = Message("lines {value}-{value2}", banner=False)

TOOL_FOLD_COUNT = Message("{value} {value2}", banner=False)

TOOL_FOLD_NOUNS = {
    "read": ("file", "files"),
    "fs_search": ("search", "searches"),
    "ast_search": ("search", "searches"),
    "exa_search": ("search", "searches"),
    "serper_search": ("search", "searches"),
    "tavily_search": ("search", "searches"),
    "docs_search": ("search", "searches"),
    "fetch": ("page", "pages"),
    "browse": ("page", "pages"),
    "shell": ("look", "looks"),
}
TOOL_FOLD_DEFAULT_NOUNS = ("call", "calls")

TOOL_LINE_NOUN = Message("line", banner=False)

TOOL_EDIT_NOUN = Message("edit", banner=False)

TOOL_FAILURE_TRIES_LEFT = Message("failed · {left} of {allowed} tries left", banner=False)

STATUS_TOOL_EXTRA = Message(" +{tool_extra}", banner=False)

STATUS_TOOL_ELAPSED = Message("{tool}{extra} {elapsed}s", banner=False)

STATUS_RESPONSE_BYTES = Message("{value:,}B", banner=False)

STATUS_RETRY = Message("retry {n}/{of} in {seconds:.0f}s", banner=False)

KEY_ACTION_SUBMIT = "Send the input line (emacs mode)."

KEY_ACTION_HISTORY_SEARCH = Message("Search every prompt in the history; pressed again, the next older match.", banner=False)

KEY_ACTION_EX_OPEN = "Open the ex line (vi normal mode)."

KEY_ACTION_EX_RUN = Message("Run the ex line.", banner=False)

KEY_ACTION_EX_CANCEL = Message("Close the ex line without running it.", banner=False)

KEY_ACTION_INTERRUPT = Message("Cancel the running turn and drop queued prompts; with none, clear the line, and pressed again, quit.", banner=False)

KEY_ACTION_EOF = Message("Delete the next character; on an empty line, quit; at the end of a line, pressed twice, quit.", banner=False)

KEY_ACTION_SUSPEND = Message("Suspend js to the shell.", banner=False)

KEY_ACTION_REASONING_TOGGLE = Message("Collapse or expand reasoning blocks.", banner=False)

KEY_ACTION_REDRAW = Message("Redraw the screen.", banner=False)

KEY_ACTION_SCROLL_UP = Message("Scroll the output up a page.", banner=False)

KEY_ACTION_SCROLL_DOWN = Message("Scroll the output down a page.", banner=False)

KEY_ACTION_COMPLETE = Message("Complete the word at the cursor, or cycle completions.", banner=False)

KEY_CONTROL_PREFIX = Message("Ctrl-", banner=False)

KEY_ESCAPE = Message("Esc", banner=False)

PASTE_MARKER_LINES = "[paste #{n} +{lines} lines]"

PASTE_MARKER_CHARACTERS = "[paste #{n} {value} chars]"

IMAGE_PLACEHOLDER = "[image #{n}]"

CONFIG_AGENT_ID_MUST_CONTAIN_ONLY_LETTERS_NUMBERS = "agent id must contain only letters, numbers, '_' or '-'"

CONFIG_SESSION_PATH_MUST_BE_AN_EXISTING_JSONL = "session path must be an existing .jsonl file: {session}"

CONFIG_SESSION_PATH_MUST_BE_INSIDE = "session path must be inside {root}: {session}"

CONFIG_SESSION_PATH_MUST_BE_INSIDE_273 = "session path must be inside {sessions_dir}: {session}"

CONFIG_SESSION_PATH_MUST_BE_A_JSONL_FILE = "session path must be a .jsonl file: {session}"

CONFIG_SESSION_MUST_IDENTIFY_AN_EXISTING_JSONL_FILE = "session must identify an existing .jsonl file: {session}"

CONFIG_SESSION_PARENT_MUST_BE_A_DIRECTORY = "session parent must be a directory: {session}"

CONFIG_SESSION_PATH_MUST_BE_A_JSONL_FILE_307 = "session path must be a .jsonl file inside {sessions_dir}: {session}"

CONFIG_AGENT_AT_IS_UNREADABLE = "agent {agent_id!r} at {candidate} is unreadable: {strerror}"

EXPECTED_REASONING_EFFORT = "expected {choices}"

EXPECTED_SHELL_QUOTED_COMMAND = Message("expected a shell-quoted command: {exc}", banner=False)

EXPECTED_COMMAND = Message("expected a command", banner=False)

SETTING_NOT_A_HEADER_WANT_NAME_VALUE = "not a header (want 'Name: value'): {entry}"

SETTING_EMPTY_KEY = "empty key: {key!r}"

LOGIN_DEFAULT_NOT_NAME_VALUE_WITH_NAME_ONE_OF = "not NAME=value with NAME one of {value}: {entry}"

LOGIN_DEFAULT_ = "{key}: {error}"

LOGIN_STORE_HELD_BY_ANOTHER_PROCESS_FOR_S = "{store} held by another process for {timeout_s:g}s"

LOGIN_STORE_EXISTS_BUT_FAILED_TO_PARSE_AS_TOML = "{path} exists but failed to parse as TOML ({exc}); refusing to overwrite it. Fix or remove the file by hand, then retry."

LOGIN_STORE_MUST_BE_A_STRING_NOT = "{name} must be a string, not {__name__}"

LOGIN_STORE_PROVIDER_ID_MUST_BE_A_NON_EMPTY = "provider_id must be a non-empty string, not {stored_id!r}"

LOGIN_STORE_PROVIDER_HEADERS_MUST_BE_A_TABLE = "provider_headers must be a table"

LOGIN_STORE_DEFAULTS_MUST_BE_A_TABLE = "defaults must be a table"

LOGIN_STORE_MODEL_DEFAULTS_MUST_BE_A_TABLE_OF = "model_defaults must be a table of tables"

LOGIN_STORE_ = "{path}: {exc}"

LOGIN_STORE__911 = "{value}: {exc}"

LOGIN_SHAPE_CHAT_COMPLETIONS = "OpenAI-compatible chat completions"

LOGIN_SHAPE_RESPONSES = "OpenAI Responses-style endpoint"

LOGIN_SHAPE_ANTHROPIC = "Anthropic-compatible endpoint"

LOGIN_SHAPE_PROXY = "CLIProxyAPI / OpenAI-compatible proxy with optional headers"

LOGIN_PICKER_ROW = "{cursor} {box}{label}{tag}"

LOGIN_PROVIDER_ROW = "{pid:<28} {name} [{source}]"

LOGIN_API_SHAPE_ROW = "{pid}  {desc}"

LOGIN_SAVED_PROVIDER_DETAIL = "{provider_id} — {value} [saved]"

LOGIN_PICKER_CURSOR = ">"

LOGIN_PICKER_SELECTED = "[x] "

LOGIN_PICKER_UNSELECTED = "[ ] "

LOGIN_SOURCE_SAVED = Message("saved", banner=False)

LOGIN_SOURCE_ENVIRONMENT = Message("env", banner=False)

LOGIN_SOURCE_REGISTRY = Message("registry", banner=False)

LOGIN_PICKER_NEEDS_AN_INTERACTIVE_TERMINAL_STDIN_AND_STDOUT = "needs an interactive terminal (stdin and stdout must be a TTY)"

CODEX_OAUTH_OPENAI_CODEX_TOKEN_RESPONSE_DID_NOT_INCLUDE = "OpenAI Codex token response did not include access_token"

CODEX_OAUTH_OPENAI_CODEX_TOKEN_RESPONSE_DID_NOT_INCLUDE_119 = "OpenAI Codex token response did not include refresh_token"

CODEX_OAUTH_OPENAI_CODEX_TOKEN_RESPONSE_DID_NOT_INCLUDE_121 = "OpenAI Codex token response did not include expires_in"

CODEX_OAUTH_OPENAI_CODEX_ACCESS_TOKEN_DID_NOT_INCLUDE = "OpenAI Codex access token did not include chatgpt_account_id"

CODEX_OAUTH_OPENAI_CODEX_TOKEN_REQUEST_FAILED = "OpenAI Codex token request failed: {detail}"

CODEX_OAUTH_OPENAI_CODEX_TOKEN_RESPONSE_WAS_NOT_A = "OpenAI Codex token response was not a JSON object"

CODEX_OAUTH_OPENAI_CODEX_OAUTH_COULD_NOT_BIND_THE = "OpenAI Codex OAuth could not bind the callback port {CALLBACK_PORT} on 127.0.0.1 or ::1"

CODEX_OAUTH_INVALID_CALLBACK = "Invalid callback"

CODEX_OAUTH_OPENAI_CODEX_OAUTH_TIMED_OUT_WAITING_FOR = "OpenAI Codex OAuth timed out waiting for browser callback"

CODEX_OAUTH_OPENAI_CODEX_OAUTH_FAILED = "OpenAI Codex OAuth failed: {received_error}"

CODEX_OAUTH_OPENAI_CODEX_OAUTH_STATE_MISMATCH = "OpenAI Codex OAuth state mismatch"

CODEX_OAUTH_OPENAI_CODEX_DEVICE_AUTHORIZATION_FAILED = "OpenAI Codex device authorization failed: {value}"

CODEX_OAUTH_OPENAI_CODEX_DEVICE_AUTHORIZATION_RESPONSE_WAS_NOT = "OpenAI Codex device authorization response was not a JSON object"

CODEX_OAUTH_OPENAI_CODEX_DEVICE_AUTHORIZATION_RESPONSE_MISSING_DEVICE = "OpenAI Codex device authorization response missing device_auth_id or user_code"

CODEX_OAUTH_OPENAI_CODEX_DEVICE_TOKEN_POLLING_FAILED = "OpenAI Codex device token polling failed: {value}"

CODEX_OAUTH_OPENAI_CODEX_DEVICE_TOKEN_RESPONSE_WAS_NOT = "OpenAI Codex device token response was not a JSON object"

CODEX_OAUTH_OPENAI_CODEX_DEVICE_TOKEN_RESPONSE_MISSING_AUTHORIZATION = "OpenAI Codex device token response missing authorization_code or code_verifier"

CODEX_OAUTH_OPENAI_CODEX_DEVICE_AUTHORIZATION_TIMED_OUT = "OpenAI Codex device authorization timed out"

CODEX_OAUTH_OPENAI_CODEX_LOGIN_HAS_NO_REFRESH_TOKEN = "OpenAI Codex login has no refresh token; run js --login openai-codex again"

CODEX_OAUTH_CANNOT_BE_READ_FIX_IT_BEFORE_THE = "{value} cannot be read; fix it before the OpenAI Codex login can refresh"

CODEX_OAUTH_CANNOT_BE_OPENED_FIX_IT_BEFORE_THE = "{value} cannot be opened ({exc}); fix it before the OpenAI Codex login can refresh"

CODEX_LOGIN_SUCCESS_HTML = "<h1>OpenAI Codex login complete</h1><p>You can close this tab and return to js.</p>"

CODEX_LOGIN_FAILURE_HTML = "<h1>OpenAI Codex login failed</h1><p>Return to js and try again.</p>"

CODEX_REFRESH_REJECTED = "OpenAI Codex refresh token was rejected ({detail}); run js --login openai-codex again"

CODEX_PROVIDER_OPENAI_CODEX_ACCESS_TOKEN_IS_MISSING_CHATGPT = "OpenAI Codex access token is missing chatgpt_account_id"

CODEX_PROVIDER_OPENAI_CODEX_REQUIRES_AN_OAUTH_ACCESS_TOKEN = "OpenAI Codex requires an OAuth access token; run js --login openai-codex"

XAI_OAUTH_XAI_OAUTH_DISCOVERY_RETURNED_A_NON_HTTPS = "xAI OAuth discovery returned a non-HTTPS {field}"

XAI_OAUTH_XAI_OAUTH_HOST_IS_NOT_ON_X = "xAI OAuth {field} host {host!r} is not on x.ai"

XAI_OAUTH_XAI_OAUTH_DISCOVERY_FAILED_HTTP = "xAI OAuth discovery failed: HTTP {status_code}: {value}"

XAI_OAUTH_XAI_OAUTH_DISCOVERY_RETURNED_MALFORMED_JSON = "xAI OAuth discovery returned malformed JSON: {exc}"

XAI_OAUTH_XAI_OAUTH_DISCOVERY_RESPONSE_WAS_NOT_A = "xAI OAuth discovery response was not a JSON object"

XAI_OAUTH_XAI_OAUTH_TOKEN_REQUEST_FAILED_HTTP = "xAI OAuth token request failed: HTTP {status_code}: {value}"

XAI_OAUTH_XAI_OAUTH_TOKEN_RESPONSE_WAS_MALFORMED_JSON = "xAI OAuth token response was malformed JSON: {exc}"

XAI_OAUTH_XAI_OAUTH_TOKEN_RESPONSE_WAS_NOT_A = "xAI OAuth token response was not a JSON object"

XAI_OAUTH_XAI_OAUTH_TOKEN_RESPONSE_OMITTED_ACCESS_TOKEN = "xAI OAuth token response omitted access_token"

XAI_OAUTH_XAI_OAUTH_CODE_EXCHANGE_OMITTED_REFRESH_TOKEN = "xAI OAuth code exchange omitted refresh_token; the login would expire in an hour"

XAI_OAUTH_CANNOT_BE_OPENED_FIX_IT_BEFORE_THE = "{value} cannot be opened ({exc}); fix it before the xAI login can refresh"

XAI_OAUTH_CANNOT_BE_READ_FIX_IT_BEFORE_THE = "{value} cannot be read; fix it before the xAI login can refresh"

XAI_LOGIN_SUCCESS_HTML = "<h1>xAI login complete</h1><p>You can close this tab and return to js.</p>"

XAI_LOGIN_FAILURE_HTML = "<h1>xAI login failed</h1><p>Return to js and try again.</p>"

XAI_REFRESH_REJECTED = "xAI OAuth refresh token was rejected ({detail}); run js --login xai-oauth again"

OAUTH_CALLBACK_COULD_NOT_BIND_THE_CALLBACK_PORT = "{label} could not bind the callback port {port}: {exc}"

OAUTH_CALLBACK_COULD_NOT_BIND_THE_CALLBACK_PORT_ON = "{label} could not bind the callback port {port} on 127.0.0.1 or ::1"

OAUTH_CALLBACK_TIMED_OUT_WAITING_FOR_THE_BROWSER_CALLBACK = "{label} timed out waiting for the browser callback"

OAUTH_CALLBACK_FAILED = "{label} failed: {received_error}"

OAUTH_CALLBACK_STATE_MISMATCH = "{label} state mismatch"

OAUTH_CALLBACK_CALLBACK_CARRIED_NO_AUTHORIZATION_CODE = "{label} callback carried no authorization code"

PROVIDER_ENDPOINT_PROVIDER_HAS_NO_BASE_URL_CONFIGURED_REFUSING = "provider {id!r} has no base URL configured; refusing to fall back to the {sdk!r} SDK default endpoint and credentials. Run `js --login {id2}` or {hint}."

PROVIDER_ENDPOINT_ENV_HINT = Message("set {value}", banner=False)

PROVIDER_ENDPOINT_CONFIGURE_HINT = Message("configure its base URL", banner=False)

ENDPOINT_SPEC_EMPTY_ENDPOINT_SPEC_EXPECTED_MODEL_KEY_SHAPE = "empty endpoint spec; expected model[:key][[shape]][[effort]]@url"

ENDPOINT_SPEC_NO_ENDPOINT_IN_EXPECTED_SOMETHING_LIKE_QWEN = "no @endpoint in {spec!r}; expected something like qwen27b@http://host:8000/v1"

ENDPOINT_SPEC_NO_ENDPOINT_AFTER_IN = "no endpoint after @ in {spec!r}"

ENDPOINT_SPEC_ENDPOINT_HAS_NO_SCHEME_WRITE_HTTP_OR = "endpoint {base_url!r} has no scheme; write http:// or https://"

ENDPOINT_SPEC_EMPTY_IN = "empty [] in {spec!r}"

ENDPOINT_SPEC_TWO_WIRE_SHAPES_IN_AND_ONE_MORE = "two wire shapes in {spec!r}: [{tag}] and one more"

ENDPOINT_SPEC_TWO_REASONING_EFFORTS_IN_AND_ONE_MORE = "two reasoning efforts in {spec!r}: [{tag}] and one more"

ENDPOINT_SPEC_UNKNOWN_TAG_IN_WIRE_SHAPES_ARE_EFFORTS = "unknown tag [{tag}] in {spec!r}; wire shapes are {value}; efforts are {value2}"

ENDPOINT_SPEC_NO_MODEL_ID_IN = "no model id in {spec!r}"

ENDPOINT_SPEC_EMPTY_API_KEY_AFTER_IN = "empty api key after ':' in {spec!r}"

ENDPOINT_SPEC_NO_MODEL_ID_BEFORE_IN = "no model id before ':' in {spec!r}"

TOOL_RELEASE_ARIA_NO_VERIFIED_RELEASE_ASSET_FOR = "aria2: no verified release asset for {system} {machine}"

TOOL_RELEASE_TERMUX_PKG_UNAVAILABLE_RESTORE_PKG_ON_PATH = "Termux: pkg unavailable; restore pkg on PATH and rerun `just install-tool-binaries`"

TOOL_RELEASE_TERMUX_PKG_INSTALL_FAILED_NO_DESKTOP_FALLBACK = "Termux: pkg install failed: {exc}; no desktop fallback"

TOOL_RELEASE_TERMUX_PKG_DID_NOT_PROVIDE_EXECUTABLE_NO = "Termux: pkg did not provide executable {candidate}; no desktop fallback"

TOOL_RELEASE_TERMUX_PACKAGE_EXECUTABLE_RESOLVES_TO_MANAGED_TOOLS = "Termux: package executable {candidate} resolves to managed tools"

TOOL_TERMUX_AST_GREP_MISSING = "ast-grep: no verified Termux release; install a native build separately"

TOOL_TERMUX_OBSCURA_MISSING = "obscura/obscura-worker: no verified Termux release; install native builds separately"

TOOL_VERIFIED_RELEASE_MISSING = "{name}: no verified release asset for {system} {machine}"

TOOL_RELEASE_GLIBC_REQUIRED = "{name}: {value} requires glibc >= {minimum}; found {value2} {version}; no verified musl asset"

TOOL_OBSCURA_RELEASE_MISSING = "obscura: no verified compatible release for {system} {machine} {libc} {version}; pinned x86_64 Linux asset requires glibc >= 2.35; ARM64 Linux/macOS archives await content verification"

TOOL_ARIA2_HOMEBREW_FAILED = "aria2: Homebrew unavailable or `brew install aria2` failed; install Homebrew (https://brew.sh) and rerun `just install`"

TOOLKIT_INCOMPLETE_PREFIX = "toolkit incomplete:\n"

SESSION_PICKER_HEADER = Message("  {value:<{_WHEN}}  {value2:<{_MODE}}  {value3:<{_AGENT}}  {value4:<{_DIR}} {value5:>{_TURNS}}  {value6:<{_LENGTH}} name", banner=False)

SESSION_COLUMN_WHEN = Message("when", banner=False)

SESSION_COLUMN_MODE = Message("mode", banner=False)

SESSION_COLUMN_AGENT = Message("agent", banner=False)

SESSION_COLUMN_DIR = Message("dir", banner=False)

SESSION_COLUMN_TURNS = Message("turns", banner=False)

SESSION_COLUMN_LENGTH = Message("length", banner=False)

AGENT_MANIFEST_DUPLICATE_FILE_FOR_EXCHANGE = "duplicate {role} file for exchange {number}: {path}"

AGENT_MANIFEST_INVALID_YAML_MANIFEST_IN = "invalid YAML manifest in {path}: {exc}"

AGENT_MANIFEST_YAML_MANIFEST_IN_MUST_BE_A_MAPPING = "YAML manifest in {path} must be a mapping"

AGENT_MANIFEST_IS_NO_LONGER_READ = "{legacy} is no longer read; {_MIGRATE_HINT}"

AGENT_MANIFEST_FRONTMATTER_IN_IS_NO_LONGER_READ = "frontmatter in {path} is no longer read; {_MIGRATE_HINT}"

AGENT_MANIFEST_SKILLS_IN_MUST_BE_A_LIST_OF = "skills in {path} must be a list of family:name entries"

AGENT_MANIFEST_SKILLS_ENTRY_IN_IS_NOT_FAMILY_NAME = "skills entry {item!r} in {path} is not family:name or family:*"

AGENT_MANIFEST_FRONTMATTER_IN_IS_MISSING_A_CLOSING = "frontmatter in {path} is missing a closing ---"

AGENT_MANIFEST_INVALID_YAML_FRONTMATTER_IN = "invalid YAML frontmatter in {path}: {exc}"

AGENT_MANIFEST_FRONTMATTER_IN_MUST_BE_A_MAPPING = "frontmatter in {path} must be a mapping"

AGENT_MANIFEST_REASONING_IN_MUST_BE_A_STRING = "reasoning in {path} must be a string"

AGENT_MANIFEST_REASONING_IN = "reasoning in {path}: {error}"

AGENT_MANIFEST_MAX_TOKENS_IN_MUST_BE_AN_INTEGER = "max_tokens in {path} must be an integer"

AGENT_MANIFEST_SAMPLING_FRONTMATTER_IN_MUST_BE_A_MAPPING = "sampling frontmatter in {path} must be a mapping"

AGENT_MANIFEST_UNKNOWN_SAMPLING_KEY_IN_ALLOWED = "unknown sampling key '{key}' in {path}; allowed: {value}"

AGENT_MANIFEST_SAMPLING_IN_MUST_BE_A_NUMBER = "sampling.{key} in {path} must be a number"

AGENT_MANIFEST_UNKNOWN_KEY_S_IN_ALLOWED = "unknown key(s) {value} in {manifest_path}; allowed: {value2}"

AGENT_MANIFEST_PROMPTS_DIRECTORY_MISSING_AT_DROP_MD_PROMPT = "prompts directory missing at {prompts_dir}. Drop .md prompt files and an optional {AGENT_MANIFEST} manifest in there."

AGENT_MANIFEST_PROMPTS_DIRECTORY_HAS_NO_MD_PROMPT_FILES = "prompts directory {prompts_dir} has no .md prompt files or {AGENT_MANIFEST}."

AGENT_MANIFEST_MIGRATION_HINT = "move its settings to agent.yaml beside the prompt (just migrate-agents)"

SKILL_FRONTMATTER_FRONTMATTER_IS_MISSING_A_CLOSING = "frontmatter is missing a closing ---"

SKILL_FRONTMATTER_INVALID_YAML_FRONTMATTER = "invalid YAML frontmatter: {value}"

SKILL_FRONTMATTER_FRONTMATTER_MUST_BE_A_MAPPING = "frontmatter must be a mapping"

SKILL_FRONTMATTER_FRONTMATTER_MUST_BE_A_STRING = "{field} frontmatter must be a string"

SKILL_FRONTMATTER_FRONTMATTER_MUST_BE_TRUE_OR_FALSE = "{field} frontmatter must be true or false"

SKILL_FRONTMATTER_TOOLS_FRONTMATTER_MUST_BE_A_LIST_OF = "tools frontmatter must be a list of strings"

SKILL_FRONTMATTER_TOOLS_FRONTMATTER_MUST_CONTAIN_SAFE_NON_EMPTY = "tools frontmatter must contain safe non-empty strings"

SKILL_FRONTMATTER_TOOLS_FRONTMATTER_CONTAINS_DUPLICATE = "tools frontmatter contains duplicate {tool!r}"

SKILL_FRONTMATTER_PATHS_FRONTMATTER_MUST_BE_A_LIST_OF = "paths frontmatter must be a list of glob strings"

SKILL_FRONTMATTER_UNSAFE_SKILL_NAME = "unsafe skill name {name!r}"

SWARM_SWARM_NAME_MUST_BE_ONE_WORD_OF = "swarm name must be one word of [A-Za-z0-9_.-]: {name!r}"

SWARM_A_MESSAGE_KIND_IS_ONE_WORD_OF = "a message kind is one word of [A-Za-z0-9_.-]: {kind!r}"

SWARM_IS_ALREADY_ON_THE_BUS = "{name} is already on the bus"

SWARM_THIS_BUS_ALREADY_HAS_SPAWNED_AGENTS_THE = "this bus already has {spawned} spawned agents; the cap is {maximum}"

SWARM_SENT = Message("sent #{seq}", banner=False)

SWARM_POSTED = Message("posted #{id}", banner=False)

SWARM_ROSTER_YOU = _PROMPTS['default_you']

SWARM_ROSTER_SILENT = _PROMPTS['default_no_answer_at_rollcall_since']

SWARM_ROSTER_ASLEEP = Message(_PROMPTS['default_asleep'], banner=False)

SWARM_ROSTER_WORKING = Message(_PROMPTS['default_working'], banner=False)

SWARM_ROSTER_EMPTY = _PROMPTS['default_nobody_yet']

SWARM_BOARD_EMPTY = _PROMPTS['default_no_tasks_on_the_board']

SWARM_BOARD_TAKEN = _PROMPTS['default_taken_by']

SWARM_BOARD_DONE = _PROMPTS['default_done_by']

SWARM_BOARD_ROW = _PROMPTS['default_text']

SWARM_BOARD_FROM = _PROMPTS['default_from']

SWARM_ROSTER_HOLDS = Message(_PROMPTS['default_holds'], banner=False)

SWARM_BOARD_OPEN = Message(_PROMPTS['default_open'], banner=False)

SWARM_RUN_THE_SPEC_NEEDS_A_ROOT_THE_BUS = "the spec needs a root: the bus directory"

SWARM_RUN_THE_SPEC_NAMES_NO_AGENTS = "the spec names no agents"

SWARM_RUN_AGENT_NAMES_REPEAT = "agent names repeat: {value}"

SWARM_RUN_FAILED = "swarm run: {exc}"

EVENT_HOOK_UNKNOWN_EVENT = "unknown event: {clean}"

EVENT_HOOK_ON_NEEDS_A_HANDLER = "on needs a handler"

EVENT_HOOK_UNKNOWN_EVENT_149 = "unknown event: {event}"

AGENT_LOCK_ANOTHER_LOCK_RUN_OF_AGENT_IS_RUNNING = "another --lock run of agent {agent_id!r} is running ({path})"

JAIL_SETTING_EMPTY_BIND = "empty bind: {spec!r}"

JAIL_SETTING_BIND_PATH_MUST_BE_ABSOLUTE_OR_START = "bind path must be absolute or start with ~: {spec!r}"

JAIL_SETTING_JAIL_BIND_IS_A_JSON_LIST_OF = "JAIL_BIND is a JSON list of \"path[:rw]\" strings"

JAIL_SETTING_HIDE_PATH_MUST_BE_ABSOLUTE_OR_START = "hide path must be absolute or start with ~: {spec!r}"

JAIL_SETTING_JAIL_HIDE_IS_A_JSON_LIST_OF = "JAIL_HIDE is a JSON list of path strings"

JAIL_SETTING_C_TARGET_IS_NOT_A_DIRECTORY = "-C target is not a directory: {directory}"

JAIL_SETTING_C_IS_NOT_A_JAIL_NAME_A = "-C / is not a jail; name a directory"

JAIL_SETTING_C_RUNS_TOOLS_UNDER_BUBBLEWRAP_AND_BWRAP = "-C runs tools under bubblewrap, and bwrap is not on PATH; install bubblewrap"

JAIL_SETTING_C_IS_INSIDE_A_PATH_HIDDEN_IN = "-C {directory} is inside {inside}, a path hidden in the jail (JAIL_HIDE)"

JAIL_SETTING_C_BUBBLEWRAP_CANNOT_START_A_JAIL = "-C: bubblewrap cannot start a jail: {failure}"

MCP_CONFIG_DUPLICATE_OBJECT_KEY = "duplicate object key: {key!r}"

MCP_CONFIG_MUST_BE_A_JSON_OBJECT = "{setting} must be a JSON object"

MCP_CONFIG_MUST_BE_AN_OBJECT_OF_STRING_VALUES = "{field} must be an object of string values"

MCP_CONFIG_MUST_BE_A_LIST_OF_NON_EMPTY = "{field} must be a list of non-empty strings"

MCP_CONFIG_MCP_SERVERS_MUST_BE_A_JSON_OBJECT = "MCP_SERVERS must be a JSON object"

MCP_CONFIG_MCP_SERVER_NAMES_MUST_BE_NON_EMPTY = "mcp server names must be non-empty strings"

MCP_CONFIG_MCP_SERVER_NAME_HAS_NO_USABLE_CHARACTERS = "mcp server name {name!r} has no usable characters"

MCP_CONFIG_MCP_SERVER_NAMES_AND_NORMALIZE_TO_THE = "mcp server names {previous!r} and {name!r} normalize to the same name"

MCP_CONFIG_MCP_SERVER_MUST_BE_AN_OBJECT = "mcp server {name!r} must be an object"

MCP_CONFIG_MCP_SERVER_HAS_UNKNOWN_FIELD = "mcp server {name!r} has unknown field {value!r}"

MCP_CONFIG_MCP_SERVER_ENABLED_MUST_BE_A_BOOLEAN = "mcp server {name!r}.enabled must be a boolean"

MCP_CONFIG_MCP_SERVER_REQUIRES_EXACTLY_ONE_OF_COMMAND = "mcp server {name!r} requires exactly one of command or url"

MCP_CONFIG_MCP_SERVER_COMMAND_MUST_BE_A_NON = "mcp server {name!r}.command must be a non-empty string"

MCP_CONFIG_MCP_SERVER_HEADERS_IS_ONLY_VALID_WITH = "mcp server {name!r}.headers is only valid with url"

MCP_CONFIG_MCP_SERVER_URL_MUST_BE_A_NON = "mcp server {name!r}.url must be a non-empty string"

MCP_CONFIG_MCP_SERVER_URL_IS_INVALID = "mcp server {name!r}.url is invalid"

MCP_CONFIG_MCP_SERVER_URL_MUST_USE_HTTP_OR = "mcp server {name!r}.url must use http or https"

MCP_CONFIG_MCP_SERVER_URL_MUST_NOT_CONTAIN_USERINFO = "mcp server {name!r}.url must not contain userinfo"

MCP_CONFIG_MCP_SERVER_STDIO_FIELDS_REQUIRE_COMMAND = "mcp server {name!r} stdio fields require command"

MCP_CONFIG_MCP_AGENT_POLICY_MUST_BE_A_JSON = "MCP_AGENT_POLICY must be a JSON object"

MCP_CONFIG_MCP_AGENT_IDS_MAY_CONTAIN_ONLY_LETTERS = "mcp agent ids may contain only letters, numbers, '_' or '-'"

MCP_CONFIG_MCP_AGENT_POLICY_MUST_BE_AN_OBJECT = "mcp agent {agent_id!r} policy must be an object"

MCP_CONFIG_MCP_AGENT_HAS_UNKNOWN_FIELD = "mcp agent {agent_id!r} has unknown field {value!r}"

MCP_CONFIG_MCP_AGENT_MUST_BE_AN_OBJECT = "mcp agent {agent_id!r}.{section} must be an object"

MCP_CONFIG_MCP_AGENT_HAS_UNKNOWN_FIELD_226 = "mcp agent {agent_id!r}.{section} has unknown field {value!r}"

MCP_CONFIG_MCP_TIMEOUT_MUST_BE_A_POSITIVE_NUMBER = "MCP_TIMEOUT must be a positive number, got {value!r}"

# Every entry by name; each knows its own, for its /fset format.
ENTRIES: dict[str, Message] = {name: value for name, value in globals().items() if isinstance(value, Message)}
for _name, _entry in ENTRIES.items():
    object.__setattr__(_entry, "name", _name)
