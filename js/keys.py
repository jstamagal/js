"""Named key actions for the REPL input and the keys file that remaps them.

The keys file (`keys.file`, unset = ~/.js/keys) is a script in jsrc's
grammar, one command per line; blank lines and `#` comments, whole-line or
after the last word, are skipped:

    bind c-f history_search      # c-f runs history_search too
    bind escape r redraw         # a sequence: Esc, then r
    unbind c-z                   # c-z does nothing js-specific

`bind KEY... ACTION` adds the key sequence to ACTION and takes it off every
other action that can fire in the same place. `unbind KEY...` takes it off
every action. Key names are prompt_toolkit's: `c-r`, `escape`, `enter`,
`tab`, `pageup`, `f5`, `space`, or one character. A bad line is reported as
one `path:line: error` line and skipped; the rest of the file still applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import KEY_ALIASES, Keys
from prompt_toolkit.search import SearchDirection

from . import messages as msgs
from . import paths
from . import setcmd

KeySequence = tuple[str, ...]
Keymap = dict[str, tuple[KeySequence, ...]]

# Where an action can fire. "screen" actions fire anywhere on the screen, so
# they share keys with nothing; "input" and "ex" never have focus together.
SCREEN, INPUT, EX = "screen", "input", "ex"


@dataclass(frozen=True)
class Action:
    name: str
    context: str
    keys: tuple[KeySequence, ...]  # the default bindings
    doc: str


ACTIONS: tuple[Action, ...] = (
    Action("submit", INPUT, (("enter",),), "Send the input line (emacs mode)."),
    Action("history_search", INPUT, (("c-r",),),
           "Search every prompt in the history; pressed again, the next older match."),
    Action("ex_open", INPUT, ((":",),), "Open the ex line (vi normal mode)."),
    Action("ex_run", EX, (("enter",),), "Run the ex line."),
    Action("ex_cancel", EX, (("escape",),), "Close the ex line without running it."),
    Action("interrupt", SCREEN, (("c-c",),), "Cancel the running turn and drop queued prompts."),
    Action("eof", SCREEN, (("c-d",),), "Delete the next character; on an empty line, quit."),
    Action("suspend", SCREEN, (("c-z",),), "Suspend js to the shell."),
    Action("reasoning_toggle", SCREEN, (("c-o",),), "Collapse or expand reasoning blocks."),
    Action("redraw", SCREEN, (("c-l",),), "Redraw the screen."),
    Action("scroll_up", SCREEN, (("pageup",),), "Scroll the output up a page."),
    Action("scroll_down", SCREEN, (("pagedown",),), "Scroll the output down a page."),
    Action("complete", SCREEN, (("tab",),), "Complete the word at the cursor, or cycle completions."),
)
ACTION_BY_NAME: dict[str, Action] = {action.name: action for action in ACTIONS}

_KEY_NAMES = frozenset(key.value for key in Keys)


def keys_file(settings: dict | None) -> Path:
    """The keys file `keys.file` names, or ~/.js/keys when it is unset."""
    from . import settings as _settings

    value = _settings.knob(settings, "keys.file")
    return Path(value).expanduser() if value else paths.keys_file()


def default_keymap() -> Keymap:
    return {action.name: action.keys for action in ACTIONS}


def _canonical(key: str) -> str:
    key = KEY_ALIASES.get(key, key)
    return " " if key == "space" else key


def key_error(key: str) -> str | None:
    """Why ``key`` is not a prompt_toolkit key name, or None when it is one."""
    canonical = _canonical(key)
    if canonical in _KEY_NAMES or len(canonical) == 1:
        return None
    return msgs.KEYS_BAD_KEY.text(key=key)


def _same(a: KeySequence, b: KeySequence) -> bool:
    return tuple(map(_canonical, a)) == tuple(map(_canonical, b))


def _overlaps(a: str, b: str) -> bool:
    return a == b or SCREEN in (a, b)


def bind(keymap: Keymap, sequence: KeySequence, action: str) -> Keymap:
    """``keymap`` with ``sequence`` added to ``action`` and taken off every
    other action that can fire in the same place."""
    context = ACTION_BY_NAME[action].context
    result: Keymap = {}
    for name, sequences in keymap.items():
        if name != action and _overlaps(ACTION_BY_NAME[name].context, context):
            sequences = tuple(seq for seq in sequences if not _same(seq, sequence))
        result[name] = sequences
    if not any(_same(seq, sequence) for seq in result[action]):
        result[action] = (*result[action], sequence)
    return result


def unbind(keymap: Keymap, sequence: KeySequence) -> Keymap:
    """``keymap`` with ``sequence`` taken off every action."""
    return {name: tuple(seq for seq in sequences if not _same(seq, sequence))
            for name, sequences in keymap.items()}


def apply_line(keymap: Keymap, line: str) -> tuple[Keymap, str | None]:
    """One keys-file line applied to ``keymap``: ``(keymap, error)``. A line
    with an error leaves the keymap as it was."""
    parsed = setcmd.split_command(line)
    if parsed is None:
        return keymap, None
    verb, arg = parsed
    words = setcmd._strip_load_comment(arg).split()
    if verb == "bind":
        if len(words) < 2:
            return keymap, msgs.KEYS_BIND_USAGE.text()
        *sequence, action = words
        if action not in ACTION_BY_NAME:
            return keymap, msgs.KEYS_UNKNOWN_ACTION.text(action=action)
    elif verb == "unbind":
        if not words:
            return keymap, msgs.KEYS_UNBIND_USAGE.text()
        sequence, action = words, None
    else:
        return keymap, msgs.KEYS_UNKNOWN_VERB.text(verb=verb)
    for key in sequence:
        if (error := key_error(key)) is not None:
            return keymap, error
    if action is None:
        return unbind(keymap, tuple(sequence)), None
    return bind(keymap, tuple(sequence), action), None


def parse(text: str, path: Path | str) -> tuple[Keymap, list[str]]:
    """The keymap a keys file's ``text`` gives, starting from the defaults, and
    one `path:line: error` line per line that did not apply."""
    keymap = default_keymap()
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        keymap, error = apply_line(keymap, line)
        if error is not None:
            errors.append(msgs.SCRIPT_LINE_FAILED.text(path=path, lineno=lineno, error=error))
    return keymap, errors


def load(path: Path) -> tuple[Keymap, list[str]]:
    """The keymap in the keys file at ``path``; the defaults when there is none."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_keymap(), []
    except (OSError, UnicodeError) as exc:
        return default_keymap(), [msgs.KEYS_UNREADABLE.text(path=path, error=exc)]
    return parse(text, path)


def describe(sequence: KeySequence) -> str:
    """A key sequence as the screen names it: `c-o` is Ctrl-O."""
    def one(key: str) -> str:
        if key.startswith("c-") and len(key) == 3:
            return "Ctrl-" + key[2:].upper()
        return {"escape": "Esc"}.get(key, key)

    return " ".join(one(key) for key in sequence)


def history_search(event) -> None:
    """Open the reverse incremental search over the input's history; while it
    is open, move to the next older match."""
    from prompt_toolkit import search

    if event.app.layout.is_searching:
        search.do_incremental_search(SearchDirection.BACKWARD, count=event.arg)
    else:
        search.start_search(direction=SearchDirection.BACKWARD)


def prompt_bindings(keymap: Keymap) -> KeyBindings:
    """The keymap's bindings a plain prompt_toolkit prompt (the --blocking
    REPL) runs: history_search. Its other keys are prompt_toolkit's own."""
    kb = KeyBindings()
    for sequence in keymap.get("history_search", ()):
        kb.add(*sequence)(history_search)
    return kb
