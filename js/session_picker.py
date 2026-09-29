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

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input, create_input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.styles import Style

from . import messages as msgs
from . import session_query as Q
from . import session_store

RESUME = "resume"
BRANCH = "branch"
# Search hits whose matching row is looked up; the rest show without one.
_HIT_LINES = 300
_PAGE = 10


@dataclass(frozen=True)
class Choice:
    """What the operator picked: resume `session`, or branch it at the message
    numbered `message`, whose record id is `message_id`."""

    action: str
    session: Q.Session
    message: int | None = None
    message_id: str | None = None


class _Closed:
    pass


CLOSED = _Closed()

_STYLE = Style.from_dict(
    {
        "title": "ansicyan bold",
        "keys": "ansigreen",
        "query": "bold",
        "parse": "ansibrightblack",
        "columns": "ansibrightblack",
        "group": "ansiblue bold",
        "selected": "reverse",
        "hit": "ansibrightblack",
        "match": "ansiyellow bold",
        "stamp": "ansibrightblack",
        "label": "ansibrightblack",
    }
)

_MOVES = {"up": -1, "k": -1, "down": 1, "j": 1, "pageup": -_PAGE, "pagedown": _PAGE}
_FIRST = ("home", "g")
_LAST = ("end", "G")


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
        lines: Callable[[str, dict[str, float]], dict[str, Any]] | None = None,
        rows: Callable[[str], list[Any]] | None = None,
        tokens: Callable[[str], int | None] | None = None,
    ) -> None:
        self.sessions = sessions
        self.cwd = str(cwd).rstrip("/") or "/"
        self.home = str(home)
        self.now = time.time() if now is None else now
        self._search = search
        self._lines = lines
        self._rows = rows
        self._tokens = tokens
        self.view = Q.VIEWS[0]
        self.show_all = False
        self.screen = "list"
        self.query_text = query
        self.query = Q.Query()
        self.items: list[Q.Item] = []
        self.hits: dict[str, Any] = {}
        self.cursor = 0
        self.message_rows: list[Any] = []
        self.message_lines: list[str] = []
        self.message_cursor = 0
        self._message_positions: dict[str, int] = {}
        self.token_count: int | None = None
        self.refresh()

    # -- state -------------------------------------------------------------------

    def refresh(self, keep: str | None = None) -> None:
        """Rebuild the list from the query, the view and `a`. The cursor stays
        on the session `keep` names when it is still listed, else goes to the top."""
        self.query = Q.parse_query(self.query_text, now=self.now, home=self.home, cwd=self.cwd)
        expression = Q.fts_expression(self.query)
        scores = self._search(expression) if expression and self._search is not None else None
        chosen = Q.select(self.sessions, self.query, show_all=self.show_all, scores=scores)
        self.items = Q.build_items(chosen, self.view, home=self.home, nested=not self.query.ranked)
        self.hits = {}
        if expression and scores and self._lines is not None:
            self.hits = self._lines(expression, {s.path: scores[s.path] for s in chosen[:_HIT_LINES]})
        selectable = self._selectable()
        self.cursor = selectable[0] if selectable else 0
        if keep is not None:
            for index in selectable:
                if self.items[index].session.path == keep:
                    self.cursor = index
                    break

    def _selectable(self) -> list[int]:
        return [index for index, item in enumerate(self.items) if item.session is not None]

    def current(self) -> Q.Session | None:
        if 0 <= self.cursor < len(self.items):
            return self.items[self.cursor].session
        return None

    def shown(self) -> list[Q.Session]:
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
        self.message_rows = list(self._rows(session.path)) if self._rows is not None else []
        self.message_lines = Q.message_lines(self.message_rows)
        last = max(0, len(self.message_rows) - 1)
        self.message_cursor = min(self._message_positions.get(session.path, last), last)
        self.screen = "messages"

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
        """Apply one key. A Choice or CLOSED ends the picker; None keeps it open."""
        if key == "c-c":
            return CLOSED
        handler = {"list": self._list_key, "search": self._search_key,
                   "messages": self._messages_key, "info": self._info_key}[self.screen]
        return handler(key)

    def _list_key(self, key: str) -> Choice | _Closed | None:
        session = self.current()
        if key in _MOVES:
            self.move(_MOVES[key])
        elif key in _FIRST or key in _LAST:
            self._jump(key in _LAST)
        elif key == "enter":
            return Choice(RESUME, session) if session is not None else None
        elif key == "v":
            self.view = Q.VIEWS[(Q.VIEWS.index(self.view) + 1) % len(Q.VIEWS)]
            self.refresh(self._keep())
        elif key == "/":
            self.screen = "search"
        elif key == "b" and session is not None:
            self._open_messages(session)
        elif key == "i" and session is not None:
            self._open_info(session)
        elif key == "a":
            self.show_all = not self.show_all
            self.refresh(self._keep())
        elif key == "escape":
            if not self.query_text:
                return CLOSED
            self.query_text = ""
            self.refresh(self._keep())
        elif key == "q":
            return CLOSED
        return None

    def _search_key(self, key: str) -> Choice | _Closed | None:
        text = self.query_text
        if key in ("up", "down", "pageup", "pagedown"):
            self.move(_MOVES[key])
            return None
        if key == "enter":
            self.screen = "list"
            return None
        if key == "escape":
            self.query_text = ""
            self.screen = "list"
        elif key == "backspace":
            self.query_text = text[:-1]
        elif key == "c-u":
            self.query_text = ""
        elif key == "c-w":
            self.query_text = text.rstrip()[: len(text.rstrip()) - len(text.rstrip().split(" ")[-1])]
        elif len(key) == 1 and key.isprintable():
            self.query_text = text + key
        if self.query_text != text:
            self.refresh()
        return None

    def _messages_key(self, key: str) -> Choice | _Closed | None:
        session = self.current()
        last = max(0, len(self.message_rows) - 1)
        if key in _MOVES:
            self.message_cursor = max(0, min(last, self.message_cursor + _MOVES[key]))
        elif key in _FIRST or key in _LAST:
            self.message_cursor = last if key in _LAST else 0
        elif key == "enter" and session is not None and self.message_rows:
            row = self.message_rows[self.message_cursor]
            return Choice(BRANCH, session, row.number, getattr(row, "id", None))
        elif key == "r" and session is not None:
            return Choice(RESUME, session)
        elif key in ("escape", "q", "b"):
            self._back()
        return None

    def _info_key(self, key: str) -> Choice | _Closed | None:
        session = self.current()
        if key == "enter" and session is not None:
            return Choice(RESUME, session)
        if key in ("escape", "q", "i"):
            self._back()
        return None

    # -- view --------------------------------------------------------------------

    def title(self) -> StyleAndTextTuples:
        session = self.current()
        if self.screen == "messages" and session is not None:
            text = msgs.SESSIONS_MESSAGES_TITLE.text(
                when=Q.when_text(session.when, self.now), agent=session.agent or "-",
                dir=Q.short_dir(session.cwd, self.home), turns=msgs.plural(session.turns, "turn"))
            keys = msgs.SESSIONS_MESSAGES_KEYS.text()
        elif self.screen == "info" and session is not None:
            text = msgs.SESSIONS_INFO_TITLE.text(when=Q.when_text(session.when, self.now),
                                                 agent=session.agent or "-",
                                                 dir=Q.short_dir(session.cwd, self.home))
            keys = msgs.SESSIONS_INFO_KEYS.text()
        else:
            scope = (msgs.SESSIONS_ALL_KINDS if self.show_all else msgs.SESSIONS_ALL_DIRS).text()
            order = (msgs.SESSIONS_RANKED if self.query.ranked else msgs.SESSIONS_NEWEST).text()
            text = msgs.SESSIONS_TITLE.text(count=len(self.shown()), view=Q.view_name(self.view),
                                            order=order, scope=scope)
            keys = (msgs.SESSIONS_QUERY_KEYS if self.screen == "search" else msgs.SESSIONS_KEYS).text()
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
        for index, item in enumerate(self.items):
            if item.session is None:
                out += [("class:group", item.group), ("", "\n")]
                line += 1
                continue
            session = item.session
            here = (session.cwd or "").rstrip("/") == self.cwd
            selected = index == self.cursor
            marker = ">" if selected else "•" if here else " "
            text = Q.session_line(item, now=self.now, home=self.home, marker=marker)
            if selected:
                cursor_line = line
            out += [("class:selected" if selected else "", text), ("", "\n")]
            line += 1
            hit = self.hits.get(session.path)
            if hit is not None and hit.line:
                out += self._hit_fragments(hit)
                out.append(("", "\n"))
                line += 1
        return out, cursor_line

    def _hit_fragments(self, hit: Any) -> StyleAndTextTuples:
        head = Q.hit_line(hit.number, hit.who, "")
        text, spans = Q.excerpt(hit.line, hit.spans)
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
        session = self.current()
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
        rows += [(msgs.SESSIONS_INFO_KIND.text(), Q.kind(session)),
                 (msgs.SESSIONS_INFO_TAGS.text(), " · ".join(session.tags) or "-")]
        return rows

    def _info_body(self) -> StyleAndTextTuples:
        out: StyleAndTextTuples = []
        for label, value in self.info_rows():
            out += [("class:label", f"  {label:<12} "), ("", value), ("", "\n")]
        return out

    def application(self, *, input: Input | None = None, output: Output | None = None
                    ) -> Application[Choice | None]:
        kb = KeyBindings()
        names = {Keys.ControlM: "enter", Keys.ControlJ: "enter", Keys.ControlH: "backspace",
                 Keys.Backspace: "backspace", Keys.Escape: "escape", Keys.ControlC: "c-c",
                 Keys.ControlU: "c-u", Keys.ControlW: "c-w", Keys.Up: "up", Keys.Down: "down",
                 Keys.PageUp: "pageup", Keys.PageDown: "pagedown", Keys.Home: "home", Keys.End: "end"}

        def dispatch(event: KeyPressEvent) -> None:
            pressed = event.key_sequence[0].key
            key = names.get(pressed, pressed if isinstance(pressed, str) and len(pressed) == 1 else None)
            if key is None:
                key = event.data if len(event.data) == 1 and event.data.isprintable() else ""
            if not key:
                return
            result = self.press(key)
            if isinstance(result, Choice):
                event.app.exit(result=result)
            elif result is CLOSED:
                event.app.exit(result=None)

        kb.add(Keys.Any)(dispatch)
        kb.add("escape", eager=True)(dispatch)

        body = FormattedTextControl(
            lambda: self.body()[0],
            get_cursor_position=lambda: Point(0, self.body()[1]),
            show_cursor=False,
        )
        root = HSplit(
            [
                Window(FormattedTextControl(self.title), height=1),
                Window(FormattedTextControl(self.subtitle), height=lambda: Dimension.exact(self.subtitle_height())),
                Window(body, wrap_lines=False, always_hide_cursor=True),
            ]
        )
        return Application(layout=Layout(root), key_bindings=kb, style=_STYLE, full_screen=True,
                           input=input, output=output)


def _tokens(path: str) -> int | None:
    from . import context_budget, memory

    try:
        return context_budget.estimate_messages_tokens(memory.load_replay_messages(Path(path)))
    except (OSError, ValueError):
        return None


def load(cwd: str | Path, query: str = "") -> SessionPicker:
    """A picker over every session in the catalog, with search, message rows
    and token counts read from the index and the session files."""
    from . import paths, session_index, session_text

    sessions = [Q.Session.from_summary(summary) for summary in session_index.catalog()]
    return SessionPicker(
        sessions,
        cwd=cwd,
        home=paths.user_home(),
        query=query,
        search=session_index.search,
        lines=session_index.matching_lines,
        rows=lambda path: session_text.rows(Path(path)),
        tokens=_tokens,
    )


def pick_session(cwd: str | Path, query: str = "") -> Choice | None:
    """Run the picker on the terminal and return the choice, or None when closed."""
    from . import picker

    state = load(cwd, query)
    app = state.application(input=create_input(sys.__stdin__), output=create_output(sys.__stdout__))
    return picker.run_modal(app)
