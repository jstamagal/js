"""Named key actions for the REPL input and the keys file that remaps them.

The keys file (`KEYBINDS_FILE`, unset = ~/.js/keys) is a script in jsrc's
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
from collections.abc import Callable, Mapping

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
    Action("submit", INPUT, (("enter",),), msgs.KEY_ACTION_SUBMIT.format()),
    Action("history_search", INPUT, (("c-r",),),
           msgs.KEY_ACTION_HISTORY_SEARCH.words()),
    Action("ex_open", INPUT, ((":",),), msgs.KEY_ACTION_EX_OPEN.format()),
    Action("ex_run", EX, (("enter",),), msgs.KEY_ACTION_EX_RUN.words()),
    Action("ex_cancel", EX, (("escape",),), msgs.KEY_ACTION_EX_CANCEL.words()),
    Action("interrupt", SCREEN, (("c-c",),), msgs.KEY_ACTION_INTERRUPT.words()),
    Action("eof", SCREEN, (("c-d",),), msgs.KEY_ACTION_EOF.words()),
    Action("suspend", SCREEN, (("c-z",),), msgs.KEY_ACTION_SUSPEND.words()),
    Action("reasoning_toggle", SCREEN, (("c-o",),), msgs.KEY_ACTION_REASONING_TOGGLE.words()),
    Action("redraw", SCREEN, (("c-l",),), msgs.KEY_ACTION_REDRAW.words()),
    Action("scroll_up", SCREEN, (("pageup",),), msgs.KEY_ACTION_SCROLL_UP.words()),
    Action("scroll_down", SCREEN, (("pagedown",),), msgs.KEY_ACTION_SCROLL_DOWN.words()),
    Action("complete", SCREEN, (("tab",),), msgs.KEY_ACTION_COMPLETE.words()),
)
ACTION_BY_NAME: dict[str, Action] = {action.name: action for action in ACTIONS}

# prompt_toolkit's pseudo-keys (`<any>`, `<sigint>`, `<bracketed-paste>`, ...) are
# not keys a person presses; `bind <any> redraw` would fire on every keypress.
_KEY_NAMES = frozenset(key.value for key in Keys if not key.value.startswith("<"))


def keys_file(settings: dict | None) -> Path:
    """The keys file `KEYBINDS_FILE` names, or ~/.js/keys when it is unset."""
    from . import settings as _settings

    value = _settings.knob(settings, "KEYBINDS_FILE")
    return Path(value).expanduser() if value else paths.keys_file()


def default_keymap(actions: tuple[Action, ...] | None = None) -> Keymap:
    return {action.name: action.keys for action in (ACTIONS if actions is None else actions)}



def action_for(keymap: Keymap, key: str | KeySequence) -> str | None:
    """The action bound to a picker key or sequence; None for search text."""
    sequence = (key,) if isinstance(key, str) else key
    return next((action for action, sequences in keymap.items()
                 if any(_same(sequence, bound) for bound in sequences)), None)

def _canonical(key: str) -> str:
    key = KEY_ALIASES.get(key, key)
    return " " if key == "space" else key


def key_error(key: str) -> str | None:
    """Why ``key`` is not a prompt_toolkit key name, or None when it is one."""
    canonical = _canonical(key)
    if canonical in _KEY_NAMES or len(canonical) == 1:
        return None
    return msgs.KEYS_BAD_KEY.said(key=key)


def _same(a: KeySequence, b: KeySequence) -> bool:
    return tuple(map(_canonical, a)) == tuple(map(_canonical, b))


def _overlaps(a: str, b: str) -> bool:
    return a == b or SCREEN in (a, b)


def bind(keymap: Keymap, sequence: KeySequence, action: str, *, actions: tuple[Action, ...] | None = None) -> Keymap:
    """``keymap`` with ``sequence`` added to ``action`` and taken off every
    other action that can fire in the same place."""
    registry = ACTION_BY_NAME if actions is None else {item.name: item for item in actions}
    context = registry[action].context
    result: Keymap = {}
    for name, sequences in keymap.items():
        if name != action and _overlaps(registry[name].context, context):
            sequences = tuple(seq for seq in sequences if not _same(seq, sequence))
        result[name] = sequences
    if not any(_same(seq, sequence) for seq in result[action]):
        result[action] = (*result[action], sequence)
    return result


def unbind(keymap: Keymap, sequence: KeySequence) -> Keymap:
    """``keymap`` with ``sequence`` taken off every action."""
    return {name: tuple(seq for seq in sequences if not _same(seq, sequence))
            for name, sequences in keymap.items()}


def apply_line(keymap: Keymap, line: str, *, actions: tuple[Action, ...] | None = None) -> tuple[Keymap, str | None]:
    """One keys-file line applied to ``keymap``: ``(keymap, error)``. A line
    with an error leaves the keymap as it was."""
    registry = ACTION_BY_NAME if actions is None else {item.name: item for item in actions}
    parsed = setcmd.split_command(line)
    if parsed is None:
        return keymap, None
    verb, arg = parsed
    # Words are split on whitespace and never quoted, so a `#` word after at
    # least one other word starts the trailing comment; a key that is a lone
    # quote stays a key, and a first word `#` is the # key.
    words = arg.split()
    for index, word in enumerate(words[1:], start=1):
        if word.startswith("#"):
            words = words[:index]
            break
    if verb == "bind":
        if len(words) < 2:
            return keymap, msgs.KEYS_BIND_USAGE.said()
        *sequence, action = words
        if action not in registry:
            return keymap, msgs.KEYS_UNKNOWN_ACTION.said(action=action)
    elif verb == "unbind":
        if not words:
            return keymap, msgs.KEYS_UNBIND_USAGE.said()
        sequence, action = words, None
    else:
        return keymap, msgs.KEYS_UNKNOWN_VERB.said(verb=verb)
    for key in sequence:
        if (error := key_error(key)) is not None:
            return keymap, error
    if action is None:
        return unbind(keymap, tuple(sequence)), None
    return bind(keymap, tuple(sequence), action, actions=actions), None


def parse(text: str, path: Path | str, *, actions: tuple[Action, ...] | None = None) -> tuple[Keymap, list[str]]:
    """The keymap a keys file's ``text`` gives, starting from the defaults, and
    one `path:line: error` line per line that did not apply."""
    keymap = default_keymap(actions)
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        keymap, error = apply_line(keymap, line, actions=actions)
        if error is not None:
            errors.append(msgs.SCRIPT_LINE_FAILED.said(path=path, lineno=lineno, error=error))
    return keymap, errors


def load(path: Path, *, actions: tuple[Action, ...] | None = None) -> tuple[Keymap, list[str]]:
    """The keymap in the keys file at ``path``; the defaults when there is none."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_keymap(actions), []
    except (OSError, UnicodeError) as exc:
        return default_keymap(actions), [msgs.KEYS_UNREADABLE.said(path=path, error=exc)]
    return parse(text, path, actions=actions)


def describe(sequence: KeySequence) -> str:
    """A key sequence as the screen names it: `c-o` is Ctrl-O."""
    def one(key: str) -> str:
        if key.startswith("c-") and len(key) == 3:
            return msgs.KEY_CONTROL_PREFIX.words() + key[2:].upper()
        return {"escape": msgs.KEY_ESCAPE.words()}.get(key, key)

    return " ".join(one(key) for key in sequence)


def history_search(event) -> None:
    """Open the reverse incremental search over the input's history; while it
    is open, move to the next older match."""
    from prompt_toolkit import search

    if event.app.layout.is_searching:
        search.do_incremental_search(SearchDirection.BACKWARD, count=event.arg)
    else:
        search.start_search(direction=SearchDirection.BACKWARD)


def prompt_bindings(keymap: Keymap, *, handlers: Mapping[str, Callable] | None = None) -> KeyBindings:
    """Bind the actions a blocking prompt supplies handlers for. The stock
    prompt supplies history_search; other keys remain prompt_toolkit's own."""
    kb = KeyBindings()
    handlers = {"history_search": history_search} if handlers is None else handlers
    for action, handler in handlers.items():
        for sequence in keymap.get(action, ()):
            kb.add(*sequence)(handler)
    return kb
