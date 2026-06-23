"""REPL Tab completion — prefix match, rotating menu. No fuzzyfind.

Routed by where the word under the cursor sits (a port of an ircII `bind ^I`):

1. first word of the line  -> command completion. A bare word gets an implicit
   leading ``/`` (``comp`` -> ``/compact``).
2. arg of ``/set``/``/show``      -> setting keys.
3. arg of ``/login``/``/provider`` -> provider ids + saved login names.
4. mid-line word that looks like a path (``/a/b`` or ``@/a/b``) -> filesystem.
5. mid-line word, anything else   -> spellcheck (backend injected via ``spell``).

Always PREFIX match (``startswith``), never fuzzy subsequence. Tab-triggered,
rotating menu is configured on the PromptSession, not here.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
from collections.abc import Callable, Iterable

from prompt_toolkit.completion import Completer, Completion

from . import events

# Real REPL commands only — what _handle_command / _handle_provider_command actually dispatch.
COMMANDS: tuple[str, ...] = (
    "/help",
    "/set",
    "/show",
    "/load",
    "/on",
    "/model",
    "/pick-model",
    "/provider",
    "/baseurl",
    "/apikey",
    "/login",
    "/logout",
    "/models",
    "/reset",
    "/wipe",
    "/persona",
    "/turns",
    "/session",
    "/compact",
    "/compact-auto",
    "/refresh-model-catalog",
    "exit",
    "quit",
    ":q",
)

_SET_CMDS = ("/set", "/show")
_NAME_CMDS = ("/login", "/provider")
_ON_CMDS = ("/on",)
_PATH_ARG_CMDS = ("/load",)
_TRAILING_TOKEN = re.compile(r"\S*$")  # run of non-space chars before the cursor


def _prefix(pool: Iterable[str], token: str) -> list[str]:
    return sorted({c for c in pool if c.startswith(token)})


def command_candidates(token: str) -> list[str]:
    """First-word completion. Prefix match; a slashless word gets an implicit
    ``/`` so ``comp`` completes to ``/compact``. Returns sorted full commands."""
    out: set[str] = set()
    for cmd in COMMANDS:
        if cmd.startswith(token):
            out.add(cmd)
        elif token and not token.startswith("/") and cmd.startswith("/" + token):
            out.add(cmd)
    return sorted(out)


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

    ``setting_keys`` is the static knob list; ``names`` is a callable returning
    provider ids + saved login names (dynamic — a login may be added mid-session);
    ``spell`` is an injected ``str -> list[str]`` suggester (None until a backend
    is wired). Keeps this module dependency-free and unit-testable.
    """

    def __init__(
        self,
        setting_keys: Iterable[str] = (),
        names: Callable[[], Iterable[str]] | None = None,
        spell: Callable[[str], list[str]] | None = None,
    ) -> None:
        self._setting_keys = tuple(setting_keys)
        self._names = names
        self._spell = spell

    def candidates(self, text_before_cursor: str) -> tuple[list[str], int]:
        """Pure-ish: (candidate list, token length) for the text left of cursor."""
        token = _TRAILING_TOKEN.search(text_before_cursor).group(0)
        before = text_before_cursor[: len(text_before_cursor) - len(token)]
        if before.strip() == "":  # nothing but whitespace before -> first word
            return command_candidates(token), len(token)
        head = before.split()[0]
        norm = head if head.startswith("/") else "/" + head
        if looks_like_path(token):
            return path_candidates(token), len(token)
        if norm in _SET_CMDS:
            return _prefix(self._setting_keys, token), len(token)
        if norm in _NAME_CMDS:
            names = list(self._names()) if self._names else []
            return _prefix(names, token), len(token)
        if norm in _ON_CMDS:
            return event_candidates(token), len(token)
        if norm in _PATH_ARG_CMDS:
            return path_candidates(token), len(token)
        if self._spell is not None:
            return self._spell(token), len(token)
        return [], len(token)

    def get_completions(self, document, complete_event):
        cands, token_len = self.candidates(document.text_before_cursor)
        for cand in cands:
            yield Completion(cand, start_position=-token_len)
