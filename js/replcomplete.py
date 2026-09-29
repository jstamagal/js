"""REPL Tab completion — prefix match, rotating menu. No fuzzyfind.

Routed by where the word under the cursor sits (a port of an ircII `bind ^I`).
First word of the line is always command completion from the command table
(a bare word gets an implicit leading ``/`` — ``comp`` -> ``/compact``). For
every other word, path detection runs FIRST — a token that looks like a path
(``/a/b`` or ``@/a/b``) always wins filesystem completion regardless of which
command it's an argument to — then the command's ``complete`` source decides:

- ``set``    -> setting keys; the word after the key completes known enum
  values instead, e.g. ``model.reasoning_effort``.
- ``keys``   -> setting keys.
- ``names``  -> provider ids + saved login names.
- ``events`` -> event names.
- ``path``   -> filesystem.
- none       -> no completion (a command argument never reaches the
  spellchecker).

A first word that is not a command is prose -> spellcheck (backend injected
via ``spell``).

Always PREFIX match (``startswith``), never fuzzy subsequence. Tab-triggered,
rotating menu is configured on the PromptSession, not here.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping

from prompt_toolkit.completion import Completer, Completion

from . import events
from . import settings as _settings

CMDCHAR = "/"
_TRAILING_TOKEN = re.compile(r"\S*$")  # run of non-space chars before the cursor

# `/set <key> <value>` — keys whose value is a known small enum get their
# legal values completed instead of falling through to no candidates.
_VALUE_ENUM_KNOBS: dict[str, tuple[str, ...]] = {
    "model.reasoning_effort": _settings.REASONING_EFFORT_VALUES,
}


def _prefix(pool: Iterable[str], token: str) -> list[str]:
    return sorted({c for c in pool if c.startswith(token)})


def value_candidates(key: str, token: str) -> list[str]:
    """Known-good values for `key` (e.g. reasoning-effort stops), prefix-matched."""
    values = _VALUE_ENUM_KNOBS.get(key)
    if values is None:
        return []
    return _prefix(values, token.lower())


def command_candidates(token: str, verbs: Iterable[str]) -> list[str]:
    """First-word completion over the table's verbs. Prefix match; a slashless
    word gets an implicit ``/`` so ``comp`` completes to ``/compact``."""
    word = token[len(CMDCHAR):] if token.startswith(CMDCHAR) else token
    return sorted(CMDCHAR + verb for verb in verbs if verb.startswith(word))


def looks_like_path(token: str) -> bool:
    """A mid-line token is a path if it starts with @ or contains a slash."""
    return token.startswith("@") or "/" in token


def path_candidates(token: str) -> list[str]:
    """Filesystem completion for a path-like token; preserves a leading @."""
    at = token.startswith("@")
    raw = token[1:] if at else token
    base = os.path.expanduser(raw)
    try:
        hits = glob.glob(base + "*")
    except OSError:
        return []
    prefix = "@" if at else ""
    return [prefix + (h + "/" if os.path.isdir(h) else h) for h in sorted(hits)]


def event_candidates(token: str) -> list[str]:
    suppress = token.startswith("^")
    raw = token[1:] if suppress else token
    prefix = "^" if suppress else ""
    return [prefix + event for event in _prefix(events.CANONICAL_EVENT_NAMES, raw)]


def hunspell_suggest(word: str, *, lang: str = "en_US") -> list[str]:
    """Spelling suggestions for one word via ``hunspell -a``. Returns [] for a
    correct word, an empty/non-alpha token, or if hunspell/the dict is missing
    (so the REPL never breaks when ``hunspell-<lang>`` isn't installed)."""
    if not word or not word.isalpha():
        return []
    try:
        proc = subprocess.run(
            ["hunspell", "-a", "-d", lang],
            input=word + "\n",
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    # `-a` (ispell pipe) output: '&' line = misspelled with suggestions:
    #   & word N offset: sug1, sug2, ...
    for line in proc.stdout.splitlines():
        if line.startswith("&") and ":" in line:
            return [s.strip() for s in line.split(":", 1)[1].split(",") if s.strip()]
    return []


class JsCompleter(Completer):
    """Routes the cursor word to command / arg / path / spell candidates.

    ``commands`` returns the command table as verb -> argument completion
    source (read per keystroke, so aliases defined mid-session complete);
    ``setting_keys`` is the static setting list; ``names`` returns provider ids
    + saved login names; ``spell`` is an injected ``str -> list[str]``
    suggester. Keeps this module dependency-free and unit-testable.
    """

    def __init__(
        self,
        commands: Callable[[], Mapping[str, str | None]],
        setting_keys: Iterable[str] = (),
        names: Callable[[], Iterable[str]] | None = None,
        spell: Callable[[str], list[str]] | None = None,
    ) -> None:
        self._commands = commands
        self._setting_keys = tuple(setting_keys)
        self._names = names
        self._spell = spell

    def candidates(self, text_before_cursor: str) -> tuple[list[str], int]:
        """Pure-ish: (candidate list, token length) for the text left of cursor."""
        token = _TRAILING_TOKEN.search(text_before_cursor).group(0)
        before = text_before_cursor[: len(text_before_cursor) - len(token)]
        table = self._commands()
        if before.strip() == "":  # nothing but whitespace before -> first word
            return command_candidates(token, table), len(token)
        words = before.split()
        head = words[0].removeprefix(CMDCHAR).lower()
        if looks_like_path(token):
            return path_candidates(token), len(token)
        if head not in table:
            return (self._spell(token) if self._spell is not None else []), len(token)
        source = table[head]
        if source == "set" and len(words) >= 2:
            return value_candidates(words[1], token), len(token)
        if source in ("set", "keys"):
            return _prefix(self._setting_keys, token), len(token)
        if source == "names":
            return _prefix(list(self._names()) if self._names else [], token), len(token)
        if source == "events":
            return event_candidates(token), len(token)
        if source == "path":
            return path_candidates(token), len(token)
        return [], len(token)

    def get_completions(self, document, complete_event):
        cands, token_len = self.candidates(document.text_before_cursor)
        for cand in cands:
            yield Completion(cand, start_position=-token_len)
