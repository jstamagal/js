"""The session picker: `/session` in the REPL and a bare `js --session`.

`SessionPicker` is the state and the keys; `application()` puts it on the
screen with prompt_toolkit, as the model picker does. What the list holds and
how a query reads is `js.session_query`; where it comes from is
`js.session_index`.

Screens: the session list (with `/` search typed into it), `b` the message
list of the highlighted session, `i` its info. The picker returns a `Choice`
to resume a session or branch one at a message, or None when closed.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input, create_input
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.styles import Style

from . import (
    context_budget,
    fset,
    keys,
    memory,
    messages as msgs,
    paths,
    picker,
    runtime,
    session_index,
    session_query as Q,
    session_store,
    session_text,
)
from .session_catalog import branch_session
from .session_text import USER

RESUME = "resume"
BRANCH = "branch"
# Search hits whose matching row is looked up; the rest show without one.
_HIT_LINES = 300
# Search expressions whose scores and matching rows a picker keeps, the least
# recently typed dropped first.
_EXPRESSIONS_KEPT = 16
# Seconds between redraws while matching rows arrive.
_HIT_REDRAW = 0.03
# Session path -> (size, mtime) of the file when it was previewed, and the preview.
_PREVIEWS: dict[str, tuple[tuple[int, int], tuple[str, str]]] = {}
_PAGE = picker.PAGE


@dataclass(frozen=True)
class Choice:
    """What the operator picked: resume `session`, or branch it at the message
    numbered `message`, whose record id is `message_id`."""

    action: str
    session: Q.Session
    message: int | None = None
    message_id: str | None = None


_Closed = picker._Closed
CLOSED = picker.CLOSED


@dataclass(frozen=True)
class HitLine:
    """What a search hit shows under its session: the number and speaker of
    its matching row, and the excerpt of that row with the matched spans in
    it (`Q.excerpt`). The row itself can run to megabytes; the excerpt is
    what is kept."""

    number: int | None
    who: str
    text: str
    spans: tuple[tuple[int, int], ...]

    @classmethod
    def of(cls, hit: Any) -> HitLine:
        text, spans = Q.excerpt(hit.line, hit.spans) if hit.line else ("", ())
        return cls(hit.number, hit.who, text, spans)


class _Reader:
    """Runs the picker's searches and reads its hits' matching rows on a
    thread of its own, so the list redraws while they run. It holds one job:
    a newer one replaces a job not yet started and ends a read of rows in
    progress, through the reader's `interrupt` when it has one, else at its
    next row. A rows job fills the dict it is given with a `HitLine` per path
    and empties its `pending` set as it goes, then clears it when it ends."""

    def __init__(self, search: Callable[[str], dict[str, float]],
                 read: Callable[[str, dict[str, float]], Iterable[tuple[str, Any]]] | None) -> None:
        self._search = search
        self._read = read
        self.changed: Callable[[], None] = lambda: None
        self._ready = threading.Condition()
        self._job: tuple[Any, ...] | None = None
        self._generation = 0
        self._running: str | None = None  # the kind of job running
        self._closed = False
        self._thread: threading.Thread | None = None
        # The expression of the search that ended last, its scores, and
        # whether it ran to the end.
        self._searched: tuple[str, dict[str, float], bool] | None = None

    def _interrupt(self) -> None:
        interrupt = getattr(self._read, "interrupt", None)
        if self._running == "rows" and interrupt is not None:
            interrupt()

    def _submit(self, job: tuple[Any, ...] | None) -> None:
        with self._ready:
            self._generation += 1
            self._interrupt()
            self._job = None if job is None else (self._generation, *job)
            if self._job is not None and self._thread is None:
                self._thread = threading.Thread(target=self._work, name="session-picker-search", daemon=True)
                self._thread.start()
            self._ready.notify_all()

    def search(self, expression: str) -> None:
        """Search for `expression`; `scores` hands the result over."""
        with self._ready:
            self._searched = None
            self._submit(("search", expression))

    def read_rows(self, expression: str | None, scores: dict[str, float], into: dict[str, HitLine],
                  pending: set[str]) -> None:
        """Read the matching rows of `scores` into `into`. None, or no scores,
        only ends the job running."""
        self._submit(("rows", expression, scores, into, pending) if expression and scores and self._read else None)

    def scores(self, expression: str, *, wait: bool) -> tuple[dict[str, float], bool] | None:
        """The scores of the search for `expression` once it has run, and
        whether it ran to the end, waiting for it with `wait`; None while it
        runs."""
        with self._ready:
            if wait:
                self._ready.wait_for(lambda: self._closed or (self._searched or ("",))[0] == expression)
            if self._searched is not None and self._searched[0] == expression:
                return self._searched[1], self._searched[2]
            return ({}, False) if self._closed else None

    def settle(self) -> None:
        """Wait until the job asked for has run."""
        with self._ready:
            self._ready.wait_for(lambda: self._job is None and self._running is None)

    def close(self) -> None:
        with self._ready:
            self._closed = True
            self._generation += 1
            self._job = None
            self._interrupt()
            self._ready.notify_all()
        if self._thread is not None:
            self._thread.join()
        close = getattr(self._read, "close", None)
        if close is not None:
            close()

    def _current(self, generation: int) -> bool:
        with self._ready:
            return generation == self._generation and not self._closed

    def _work(self) -> None:
        while True:
            with self._ready:
                self._ready.wait_for(lambda: self._job is not None or self._closed)
                if self._closed:
                    return
                generation, kind, *job = self._job
                self._job = None
                self._running = kind
            try:
                if kind == "search":
                    self._run_search(*job)
                else:
                    self._run_rows(generation, *job)
            finally:
                with self._ready:
                    self._running = None
                    self._ready.notify_all()
            self.changed()

    def _run_search(self, expression: str) -> None:
        try:
            scores, whole = self._search(expression), True
        except Exception:  # noqa: BLE001 - a failed search lists nothing; it must not end the picker
            scores, whole = {}, False
        with self._ready:
            self._searched = (expression, scores, whole)

    def _run_rows(self, generation: int, expression: str, scores: dict[str, float],
                  into: dict[str, HitLine], pending: set[str]) -> None:
        shown = 0.0
        try:
            for attempt in range(2):
                try:
                    for path, hit in self._read(expression, scores):
                        if not self._current(generation):
                            break
                        into[path] = HitLine.of(hit)
                        pending.discard(path)
                        if time.monotonic() - shown >= _HIT_REDRAW:
                            shown = time.monotonic()
                            self.changed()
                    return
                except sqlite3.OperationalError as exc:
                    # An interrupt meant for the read before this one is
                    # read again once.
                    if (exc.sqlite_errorcode != sqlite3.SQLITE_INTERRUPT or not self._current(generation)
                            or attempt):
                        return
                    scores = {path: score for path, score in scores.items() if path not in into}
        except Exception:  # noqa: BLE001 - a failed read shows no matching rows; it must not end the picker
            pass
        finally:
            pending.clear()


def _style() -> Style:
    """The picker in /fset's colours."""
    c = {role: fset.hex_for(role) or "" for role in ("COLOR_ACCENT", "COLOR_OK", "COLOR_DIM", "COLOR_WARN")}
    return Style.from_dict(
        {
            "title": f"{c['COLOR_ACCENT']} bold",
            "keys": c["COLOR_OK"],
            "query": "bold",
            "parse": c["COLOR_DIM"],
            "columns": c["COLOR_DIM"],
            "group": f"{c['COLOR_ACCENT']} bold",
            "selected": "reverse",
            "hit": c["COLOR_DIM"],
            "match": f"{c['COLOR_WARN']} bold",
            "stamp": c["COLOR_DIM"],
            "label": c["COLOR_DIM"],
            "preview": c["COLOR_DIM"],
        }
    )

_MOVES = picker.MOVES
_NAVIGATION: keys.Keymap = {
    "up": (("up",), ("k",)), "down": (("down",), ("j",)),
    "page_up": (("pageup",),), "page_down": (("pagedown",),),
    "first": (("home",), ("g",)), "last": (("end",), ("G",)),
}
_COMMON: keys.Keymap = {"interrupt": (("c-c",),), "choose": (("enter",),)}
SESSION_KEYMAPS: dict[str, keys.Keymap] = {
    "list": {**_COMMON, **_NAVIGATION, "view": (("v",),), "search": (("/",),),
             "messages": (("b",),), "info": (("i",),), "all": (("a",),),
             "back": (("escape",),), "quit": (("q",),)},
    "search": {**_COMMON, "up": (("up",),), "down": (("down",),),
               "page_up": (("pageup",),), "page_down": (("pagedown",),),
               "back": (("escape",),), "backspace": (("backspace",),),
               "clear": (("c-u",),), "delete_word": (("c-w",),)},
    "messages": {**_COMMON, **_NAVIGATION, "resume": (("r",),),
                 "back": (("escape",), ("q",), ("b",))},
    "info": {**_COMMON, "back": (("escape",), ("q",), ("i",))},
}


class SessionPicker:
    """The picker's state. `press(key)` drives it: a key name as prompt_toolkit
    spells it (`up`, `enter`, `escape`, `backspace`, `c-u`) or a character."""

    def __init__(
        self,
        sessions: list[Q.Session],
        *,
        cwd: str | Path,
        home: str | Path,
        now: float | None = None,
        query: str = "",
        search: Callable[[str], dict[str, float]] | None = None,
        lines: Callable[[str, dict[str, float]], Iterable[tuple[str, Any]]] | None = None,
        rows: Callable[[str], list[Any]] | None = None,
        tokens: Callable[[str], int | None] | None = None,
        messages: Callable[[str], list[dict]] | None = None,
        keymaps: dict[str, keys.Keymap] | None = None,
    ) -> None:
        self.keymaps = SESSION_KEYMAPS if keymaps is None else keymaps
        self.sessions = sessions
        self.cwd = str(cwd).rstrip("/") or "/"
        self.home = str(home)
        self.now = time.time() if now is None else now
        self._search = search
        self._lines = lines
        self._rows = rows
        self._tokens = tokens
        self._messages = messages
        self.view = Q.VIEWS[0]
        self.show_all = False
        self.screen = "list"
        self.query_text = query
        self.query = Q.Query()
        self.items: list[Q.Item] = []
        self.hits: dict[str, HitLine] = {}
        self.cursor = 0
        self.message_rows: list[Any] = []
        self.message_lines: list[str] = []
        self.message_cursor = 0
        self._message_positions: dict[str, int] = {}
        self.token_count: int | None = None
        self._previews: dict[str, tuple[str, str]] = {}
        # Session path, nesting depth, last-branch flag and marker -> the row
        # text, drawn under the kind rules `_rules`.
        self._row_texts: dict[tuple[str, int, bool, str], str] = {}
        self._rules: Q.KindRules | None = None
        # Search expression -> its scores and the matching rows read so far.
        self._found: OrderedDict[str, tuple[dict[str, float], dict[str, HitLine]]] = OrderedDict()
        # The listed hits that show their matching row: the first _HIT_LINES.
        self._hit_paths: set[str] = set()
        # Paths whose matching row is still being read: each keeps a line for it.
        self._pending: set[str] = set()
        self._reader = _Reader(search, lines) if search is not None else None
        # The search expression whose scores the list waits for, and the
        # session the cursor is to stay on when they come.
        self._awaiting: tuple[str, str | None] | None = None
        self.refresh()
        self._take_scores(wait=True)

    # -- state -------------------------------------------------------------------

    def refresh(self, keep: str | None = None) -> None:
        """Rebuild the list from the query, the view and `a`. The cursor stays
        on the session `keep` names when it is still listed, else goes to the top."""
        self.query = Q.parse_query(self.query_text, now=self.now, home=self.home, cwd=self.cwd)
        expression = Q.fts_expression(self.query)
        self._awaiting = None
        if expression and self._reader is not None and expression not in self._found:
            # The list on the screen stays until the search's scores come.
            self._awaiting = (expression, keep)
            self._reader.search(expression)
            return
        self._build(expression, keep)

    def _build(self, expression: str | None, keep: str | None,
               found: tuple[dict[str, float], dict[str, HitLine]] | None = None) -> None:
        if found is None and expression in self._found:
            found = self._found[expression]
            self._found.move_to_end(expression)
        scores, hits = found if found is not None else (None, {})
        chosen = Q.select(self.sessions, self.query, show_all=self.show_all, scores=scores)
        self.items = Q.build_items(chosen, self.view, home=self.home, nested=not self.query.ranked)
        self.hits = hits
        self._pending = set()
        self._hit_paths = {s.path for s in chosen[:_HIT_LINES]} if scores else set()
        wanted = {path: scores[path] for path in self._hit_paths if path not in hits}
        if self._reader is not None:
            self._pending = set(wanted)
            self._reader.read_rows(expression, wanted, hits, self._pending)
        selectable = self._selectable()
        self.cursor = selectable[0] if selectable else 0
        if keep is not None:
            for index in selectable:
                if self.items[index].session.path == keep:
                    self.cursor = index
                    break

    def _take_scores(self, *, wait: bool) -> None:
        """Build the list from the awaited search's scores once they have
        come, waiting for them with `wait`. The expressions typed last keep
        their scores and matching rows."""
        if self._awaiting is None or self._reader is None:
            return
        expression, keep = self._awaiting
        searched = self._reader.scores(expression, wait=wait)
        if searched is None:
            return
        self._awaiting = None
        scores, whole = searched
        if not whole:  # a failed search lists nothing now and runs again when typed again
            self._build(expression, keep, (scores, {}))
            return
        self._found[expression] = (scores, {})
        while len(self._found) > _EXPRESSIONS_KEPT:
            self._found.popitem(last=False)
        self._build(expression, keep)

    def settle(self) -> None:
        """Wait until the search has run and the matching rows of the listed
        hits have been read."""
        if self._reader is not None:
            self._take_scores(wait=True)
            self._reader.settle()

    def close(self) -> None:
        """Stop searching and reading matching rows, and let go of what reads them."""
        if self._reader is not None:
            self._reader.close()

    def _selectable(self) -> list[int]:
        return [index for index, item in enumerate(self.items) if item.session is not None]

    def current(self) -> Q.Session | None:
        """The highlighted session, once a search typed has listed its hits."""
        self._take_scores(wait=True)
        return self._current()

    def _current(self) -> Q.Session | None:
        if 0 <= self.cursor < len(self.items):
            return self.items[self.cursor].session
        return None

    def shown(self) -> list[Q.Session]:
        """The listed sessions, once a search typed has listed its hits."""
        self._take_scores(wait=True)
        return self._shown()

    def _shown(self) -> list[Q.Session]:
        return [item.session for item in self.items if item.session is not None]

    def move(self, delta: int) -> None:
        selectable = self._selectable()
        if not selectable:
            return
        position = selectable.index(self.cursor) if self.cursor in selectable else 0
        self.cursor = selectable[max(0, min(len(selectable) - 1, position + delta))]

    def _jump(self, last: bool) -> None:
        selectable = self._selectable()
        if selectable:
            self.cursor = selectable[-1 if last else 0]

    def _keep(self) -> str | None:
        session = self.current()
        return session.path if session is not None else None

    def _open_messages(self, session: Q.Session) -> None:
        # The file can vanish (deleted, moved, unmounted) while the picker is
        # open; an empty list beats killing the full-screen app.
        try:
            rows = list(self._rows(session.path)) if self._rows is not None else []
        except (OSError, ValueError):
            rows = []
        self.message_rows = rows
        self.message_lines = Q.message_lines(self.message_rows)
        last = max(0, len(self.message_rows) - 1)
        self.message_cursor = min(self._message_positions.get(session.path, last), last)
        self.screen = "messages"

    def preview(self, session: Q.Session) -> tuple[str, str]:
        """The session's last user text and agent line. A file is read again
        only when its size or mtime changed since it was last previewed; a
        session with no file to stat is read once per picker."""
        stamp = paths.file_stamp(session.path)
        cached = _PREVIEWS.get(session.path)
        if stamp is None and session.path in self._previews:
            preview = self._previews[session.path]
        elif stamp is not None and cached is not None and cached[0] == stamp:
            preview = cached[1]
        else:
            try:
                messages = list(self._messages(session.path)) if self._messages is not None else []
            except (OSError, ValueError):
                messages = []
            preview = Q.preview(messages)
            if stamp is not None:
                _PREVIEWS[session.path] = (stamp, preview)
        self._previews[session.path] = preview
        return preview

    def preview_height(self) -> int:
        """Lines the highlighted session's preview takes under its row."""
        self._take_scores(wait=False)
        session = self._current()
        if self.screen not in ("list", "search") or session is None:
            return 0
        return sum(1 for said in self.preview(session) if said)

    def _open_info(self, session: Q.Session) -> None:
        self.token_count = self._tokens(session.path) if self._tokens is not None else None
        self.screen = "info"

    def _back(self) -> None:
        session = self.current()
        if self.screen == "messages" and session is not None:
            self._message_positions[session.path] = self.message_cursor
        self.screen = "list"

    # -- keys --------------------------------------------------------------------

    def press(self, key: str) -> Choice | _Closed | None:
        """Apply one key. A Choice or CLOSED ends the picker; None keeps it open.
        A key typed into the search goes in at once; any other key first waits
        for the list of the search typed."""
        action = keys.action_for(self.keymaps[self.screen], key)
        if action == "interrupt":
            return CLOSED
        if self.screen != "search" or action in _MOVES:
            self._take_scores(wait=True)
        handler = {"list": self._list_key, "search": self._search_key,
                   "messages": self._messages_key, "info": self._info_key}[self.screen]
        return handler(key)

    def _list_key(self, key: str) -> Choice | _Closed | None:
        action = keys.action_for(self.keymaps[self.screen], key)
        session = self.current()
        if action in _MOVES:
            self.move(_MOVES[action])
        elif action in ("first", "last"):
            self._jump(action == "last")
        elif action == "choose":
            return Choice(RESUME, session) if session is not None else None
        elif action == "view":
            self.view = Q.VIEWS[(Q.VIEWS.index(self.view) + 1) % len(Q.VIEWS)]
            self.refresh(self._keep())
        elif action == "search":
            self.screen = "search"
        elif action == "messages" and session is not None:
            self._open_messages(session)
        elif action == "info" and session is not None:
            self._open_info(session)
        elif action == "all":
            self.show_all = not self.show_all
            self.refresh(self._keep())
        elif action == "back":
            if not self.query_text:
                return CLOSED
            self.query_text = ""
            self.refresh(self._keep())
        elif action == "quit":
            return CLOSED
        return None

    def _search_key(self, key: str) -> Choice | _Closed | None:
        picker.search_key(self, key)
        return None

    def _messages_key(self, key: str) -> Choice | _Closed | None:
        action = keys.action_for(self.keymaps[self.screen], key)
        session = self.current()
        last = max(0, len(self.message_rows) - 1)
        if action in _MOVES:
            self.message_cursor = max(0, min(last, self.message_cursor + _MOVES[action]))
        elif action in ("first", "last"):
            self.message_cursor = last if action == "last" else 0
        elif action == "choose" and session is not None and self.message_rows:
            row = self.message_rows[self.message_cursor]
            return Choice(BRANCH, session, row.number, getattr(row, "id", None))
        elif action == "resume" and session is not None:
            return Choice(RESUME, session)
        elif action == "back":
            self._back()
        return None

    def _info_key(self, key: str) -> Choice | _Closed | None:
        action = keys.action_for(self.keymaps[self.screen], key)
        session = self.current()
        if action == "choose" and session is not None:
            return Choice(RESUME, session)
        if action == "back":
            self._back()
        return None

    # -- view --------------------------------------------------------------------

    def title(self) -> StyleAndTextTuples:
        self._take_scores(wait=False)
        session = self._current()
        if self.screen == "messages" and session is not None:
            text = msgs.SESSIONS_MESSAGES_TITLE.text(
                when=Q.when_text(session.when, self.now), agent=session.agent or "-",
                dir=Q.short_dir(session.cwd, self.home), turns=msgs.plural(session.turns, "turn"))
            keys = msgs.SESSIONS_MESSAGES_KEYS.with_keymap(self.keymaps[self.screen], SESSION_KEYMAPS[self.screen]).text()
        elif self.screen == "info" and session is not None:
            text = msgs.SESSIONS_INFO_TITLE.text(when=Q.when_text(session.when, self.now),
                                                 agent=session.agent or "-",
                                                 dir=Q.short_dir(session.cwd, self.home))
            keys = msgs.SESSIONS_INFO_KEYS.with_keymap(self.keymaps[self.screen], SESSION_KEYMAPS[self.screen]).text()
        else:
            scope = (msgs.SESSIONS_ALL_KINDS if self.show_all else msgs.SESSIONS_ALL_DIRS).text()
            order = (msgs.SESSIONS_RANKED if self.query.ranked else msgs.SESSIONS_NEWEST).text()
            text = msgs.SESSIONS_TITLE.text(count=len(self._shown()), view=Q.view_name(self.view),
                                            order=order, scope=scope)
            keys = (msgs.SESSIONS_QUERY_KEYS if self.screen == "search" else msgs.SESSIONS_KEYS).with_keymap(self.keymaps[self.screen], SESSION_KEYMAPS[self.screen]).text()
        return [("class:title", text), ("", "    "), ("class:keys", keys)]

    def subtitle(self) -> StyleAndTextTuples:
        """The query line, how it was read, and the column heading: list screens only."""
        if self.screen not in ("list", "search"):
            return []
        out: StyleAndTextTuples = []
        if self.screen == "search" or self.query_text:
            cursor = "▏" if self.screen == "search" else ""
            out += [("class:query", f" / {self.query_text}{cursor}"), ("", "\n"),
                    ("class:parse", f"   {Q.describe(self.query)}"), ("", "\n")]
        out.append(("class:columns", Q.header_line()))
        return out

    def subtitle_height(self) -> int:
        if self.screen not in ("list", "search"):
            return 0
        return 3 if self.screen == "search" or self.query_text else 1

    def body(self) -> tuple[StyleAndTextTuples, int]:
        """The body's fragments and the line the cursor is on."""
        self._take_scores(wait=False)
        if self.screen == "messages":
            return self._message_body()
        if self.screen == "info":
            return self._info_body(), 0
        return self._list_body()

    def _list_body(self) -> tuple[StyleAndTextTuples, int]:
        out: StyleAndTextTuples = []
        cursor_line = 0
        line = 0
        if not self.items:
            return [("class:parse", f"  {msgs.SESSIONS_NONE.text()}")], 0
        rules = Q.KindRules.current()
        if rules != self._rules:
            self._rules = rules
            self._row_texts = {}
        for index, item in enumerate(self.items):
            if item.session is None:
                out += [("class:group", item.group), ("", "\n")]
                line += 1
                continue
            session = item.session
            here = bool(session.cwd) and (session.cwd.rstrip("/") or "/") == self.cwd
            selected = index == self.cursor
            marker = ">" if selected else "•" if here else " "
            key = (session.path, item.depth, item.last, marker)
            text = self._row_texts.get(key)
            if text is None:
                text = Q.session_line(item, now=self.now, home=self.home, marker=marker, rules=rules)
                self._row_texts[key] = text
            if selected:
                cursor_line = line
            out += [("class:selected" if selected else "", text), ("", "\n")]
            line += 1
            if selected:
                for who, said in zip((USER, session.agent or "-"), self.preview(session)):
                    if said:
                        out += [("class:preview", f"        {who}: {said}"), ("", "\n")]
                        line += 1
            hit = self.hits.get(session.path) if session.path in self._hit_paths else None
            if hit is not None and hit.text:
                out += self._hit_fragments(hit)
                out.append(("", "\n"))
                line += 1
            elif hit is None and session.path in self._pending:
                out.append(("", "\n"))
                line += 1
        return out, cursor_line

    def _hit_fragments(self, hit: HitLine) -> StyleAndTextTuples:
        head = Q.hit_line(hit.number, hit.who, "")
        text, spans = hit.text, hit.spans
        out: StyleAndTextTuples = [("class:hit", f"        {head}")]
        position = 0
        for start, end in spans:
            out += [("class:hit", text[position:start]), ("class:match", text[start:end])]
            position = end
        out.append(("class:hit", text[position:]))
        return out

    def _message_body(self) -> tuple[StyleAndTextTuples, int]:
        if not self.message_lines:
            return [("class:parse", f"  {msgs.SESSIONS_MESSAGES_NONE.text()}")], 0
        out: StyleAndTextTuples = []
        for index, text in enumerate(self.message_lines):
            selected = index == self.message_cursor
            out += [("class:selected" if selected else "", f"{'>' if selected else ' '} {text}"), ("", "\n")]
        return out, self.message_cursor

    def info_rows(self) -> list[tuple[str, str]]:
        session = self._current()
        if session is None:
            return []
        stamp = session.last_stamp or {}
        changes = session.model_changes or tuple((model, 0) for model in session.models)
        models = " → ".join(f"{model} #{number:04d}" if number else model for model, number in changes)
        branch = "-"
        if session.branch_of:
            branch = msgs.SESSIONS_INFO_BRANCH_VALUE.text(
                parent=session_store.display_name(Path(session.branch_of)),
                point=session_store.point_label(session.branch_point) if session.branch_point is not None else "-")
        rows = [
            (msgs.SESSIONS_INFO_FILE.text(), session.path),
            (msgs.SESSIONS_INFO_TITLE_ROW.text(), session.title or "-"),
            (msgs.SESSIONS_INFO_AGENT.text(), session.agent or "-"),
            (msgs.SESSIONS_INFO_DIR.text(), session.cwd or "-"),
            (msgs.SESSIONS_INFO_MODE.text(), session.mode or "-"),
            (msgs.SESSIONS_INFO_COMMAND.text(), " ".join(session.command) or "-"),
            (msgs.SESSIONS_INFO_TIMES.text(), msgs.SESSIONS_INFO_TIMES_VALUE.text(
                started=Q.when_text(session.started, self.now), last=Q.when_text(session.last, self.now))),
            (msgs.SESSIONS_INFO_SIZE.text(), msgs.SESSIONS_INFO_SIZE_VALUE.text(
                turns=msgs.plural(session.turns, "turn"), messages=msgs.plural(session.messages, "message"),
                calls=msgs.plural(session.tool_calls, "tool call"))),
            (msgs.SESSIONS_INFO_MODELS.text(), models or "-"),
            (msgs.SESSIONS_INFO_STAMP.text(), " · ".join(f"{key} {value}" for key, value in stamp.items()
                                                         if value) or "-"),
            (msgs.SESSIONS_INFO_TOKENS.text(), msgs.SESSIONS_INFO_TOKENS_VALUE.text(tokens=f"{self.token_count:,}")
             if self.token_count is not None else "-"),
            (msgs.SESSIONS_INFO_BRANCH.text(), branch),
        ]
        if session.parent:
            rows.append((msgs.SESSIONS_INFO_PARENT.text(), session.parent))
        rows.append((msgs.SESSIONS_INFO_KIND.text(), Q.kind(session)))
        return rows

    def _info_body(self) -> StyleAndTextTuples:
        out: StyleAndTextTuples = []
        for label, value in self.info_rows():
            out += [("class:label", f"  {label:<12} "), ("", value), ("", "\n")]
        return out

    def application(self, *, input: Input | None = None, output: Output | None = None
                    ) -> Application[Choice | None]:
        # The highlighted session's preview sits under its row: keep that
        # many lines below the cursor on the screen.
        app = picker.list_application(self, _style(), rows_below=self.preview_height, input=input, output=output)
        if self._reader is not None:
            self._reader.changed = app.invalidate
        return app


def _tokens(path: str) -> int | None:
    try:
        return context_budget.estimate_messages_tokens(memory.load_replay_messages(Path(path)))
    except (OSError, ValueError):
        return None


def _messages(path: str) -> list[dict]:
    return memory.retained_messages(Path(path))


def load(cwd: str | Path, query: str = "") -> SessionPicker:
    """A picker over every session in the catalog, with search, message rows,
    previews and token counts read from the index and the session files."""

    sessions = [Q.Session.from_summary(summary) for summary in session_index.catalog()]
    return SessionPicker(
        sessions,
        cwd=cwd,
        home=paths.user_home(),
        query=query,
        search=session_index.search,
        lines=session_index.LineReader(),
        rows=lambda path: session_text.rows(Path(path)),
        tokens=_tokens,
        messages=_messages,
    )


def pick_session(cwd: str | Path, query: str = "", *, input=None, output=None, load_picker=None) -> Choice | None:
    """Run the picker on the terminal and return the choice, or None when closed."""

    factory = load if load_picker is None else load_picker
    state = factory(cwd, query)
    try:
        app = state.application(input=create_input(sys.__stdin__) if input is None else input,
                                output=create_output(sys.__stdout__) if output is None else output)
        return picker.run_modal(app)
    finally:
        state.close()


@dataclass(frozen=True)
class SessionTarget:
    """A session the picker chose to continue in: its file, start directory and
    agent, and whether it started under a -C jail rooted at that directory."""

    path: Path
    cwd: str | None
    agent: str | None
    jailed: bool = False


def session_target(choice: Choice) -> SessionTarget:
    """The session a picker choice continues in. A branch choice creates the
    branch first, in the parent's folder."""
    session = choice.session
    path = Path(session.path)
    if choice.action == BRANCH and choice.message_id is not None:
        branch = branch_session(path, choice.message_id, cwd=session.cwd or Path.cwd(),
                                agent=session.agent, mode="repl")
        msgs.say(msgs.SESSIONS_BRANCHED, parent=session_store.display_name(path),
                 point=session_store.point_label(choice.message) if choice.message is not None else "-",
                 name=session_store.display_name(branch))
        path = branch
    return SessionTarget(path, session.cwd, session.agent, session.jailed)


def enter_session_dir(target: SessionTarget) -> None:
    """Move to the directory the session started in, when it still exists."""
    if not target.cwd:
        return
    if not Path(target.cwd).is_dir():
        msgs.warn(msgs.SESSIONS_DIR_GONE, dir=target.cwd, cwd=Path.cwd())
        return
    os.chdir(target.cwd)
    runtime.T.STOCK_CONTEXT.cwd = Path.cwd()
