"""What the session picker lists, and how its search query reads.

Pure functions over the catalog's session summaries (`js.session_index`):
the kind of a session, the query grammar, the filters, the order, the views
with branches nested under their parents, and the text of each row. Policy
comes from registered settings; the caller passes `now`, `home` and `cwd`.

Query grammar, terms combined with AND:

    niri motherboard            words, ranked with BM25 over the conversation text
    "exact phrase"              a phrase
    >10  <2  >=10,<=20          turn count
    today yesterday week        date: the session was active in that span
    2026  2026-09  2026-09-29
    today:niri                  the same as `today niri`
    agent:GLOB                  agent name
    dir:~/js                    started in exactly ~/js
    dir:~/js/*  dir:~/js/**     one level under ~/js / ~/js or anywhere under it
    mode:-p  mode:quick         how it was started, or its kind
    model:GLOB                  any message stamped with a matching model
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from . import messages as msgs
from . import session_store, settings as _settings
from .memory import HARNESS_USER_TAGS

# Kinds. Every kind but SHOWN is hidden until `a`.
SHOWN = "shown"
EMPTY = "empty"
QUICK = "quick"
SUBAGENT = "subagent"
SCRIPT = "script"



VIEWS = ("flat", "dir", "agent")
_VIEW_NAMES = {"flat": msgs.SESSIONS_VIEW_FLAT, "dir": msgs.SESSIONS_VIEW_DIR, "agent": msgs.SESSIONS_VIEW_AGENT}

_GLOB = re.compile(r"[*?\[]")
_COUNT = re.compile(r"(>=|<=|>|<|=)(\d+)")
_DAY = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
_DATE_WORDS = ("today", "yesterday", "week")
_FILTERS = ("agent", "dir", "mode", "model")
_TOKEN = re.compile(r'"([^"]*)"?|(\S+)')


@dataclass(frozen=True, eq=False)
class Session:
    """One session as the catalog summarises it."""

    path: str
    agent: str | None = None
    cwd: str | None = None
    mode: str | None = None
    command: tuple[str, ...] = ()
    title: str | None = None
    parent: str | None = None
    branch_of: str | None = None
    branch_point: Any = None
    started: float | None = None
    last: float | None = None
    mtime: float | None = None
    turns: int = 0
    messages: int = 0
    tool_calls: int = 0
    replied: bool = False
    final_len: int = 0
    models: tuple[str, ...] = ()
    model_changes: tuple[tuple[str, int], ...] = ()
    last_stamp: dict | None = None

    @classmethod
    def from_summary(cls, summary: dict[str, Any]) -> Session:
        def text(key: str) -> str | None:
            value = summary.get(key)
            return value if isinstance(value, str) and value else None

        def number(key: str) -> float | None:
            value = summary.get(key)
            return float(value) if isinstance(value, (int, float)) else None

        def count(key: str) -> int:
            value = summary.get(key)
            return int(value) if isinstance(value, (int, float)) else 0

        changes = tuple((str(pair[0]), int(pair[1])) for pair in summary.get("model_changes") or ()
                        if isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[1], int))
        stamp = summary.get("last_stamp")
        return cls(
            path=str(summary.get("path") or ""),
            agent=text("agent"),
            cwd=text("cwd"),
            mode=text("mode"),
            command=tuple(str(part) for part in summary.get("command") or ()),
            title=text("title"),
            parent=text("parent"),
            branch_of=text("branch_of"),
            branch_point=summary.get("branch_point"),
            started=number("started"),
            last=number("last"),
            mtime=number("mtime"),
            turns=count("turns"),
            messages=count("messages"),
            tool_calls=count("tool_calls"),
            replied=bool(summary.get("replied")),
            final_len=count("final_len"),
            models=tuple(str(model) for model in summary.get("models") or () if model),
            model_changes=changes,
            last_stamp=stamp if isinstance(stamp, dict) else None,
        )

    @property
    def when(self) -> float:
        """When the session started."""
        for value in (self.started, self.last, self.mtime):
            if value is not None:
                return value
        return 0.0

    @property
    def active(self) -> float:
        """When the session was last written to, as the list orders it. Tags
        are written after a session ends and do not count (`js.session_text`)."""
        return self.last if self.last is not None else self.when

    @property
    def name(self) -> str | None:
        """The name `--session` takes for a session that was given one; None
        for a generated name."""
        if not self.path or not session_store.is_named(Path(self.path).stem):
            return None
        return session_store.display_name(Path(self.path))

    @property
    def jailed(self) -> bool:
        """Whether the session started under `-C`, which jails it in its start
        directory."""
        return any(part == "-C" or (part.startswith("-C") and not part.startswith("--"))
                   for part in self.command[1:])

    @property
    def model(self) -> str | None:
        """The model of the last stamp: what a resume comes back on."""
        stamp = self.last_stamp or {}
        model = stamp.get("model")
        return model if isinstance(model, str) and model else (self.models[-1] if self.models else None)


def kind(session: Session) -> str:
    """Subagent and script-started runs first, then empty (nothing came back),
    then quick (one message, at most two tool calls, a short final reply). A
    subagent run filed before mode and parent were recorded is known by its
    task name."""
    if (session.mode == "subagent" or session.parent is not None
            or session_store.is_task(Path(session.path).stem)):
        return SUBAGENT
    if session.mode in _settings.live_value("SESSION_SCRIPT_MODES"):
        return SCRIPT
    if not session.replied:
        return EMPTY
    if (session.turns <= int(_settings.live_value("SESSION_QUICK_TURNS_MAX"))
            and session.tool_calls <= int(_settings.live_value("SESSION_QUICK_TOOL_CALLS_MAX"))
            and session.final_len < int(_settings.live_value("SESSION_QUICK_REPLY_CHARACTERS_MAX"))):
        return QUICK
    return SHOWN


# --- the query -----------------------------------------------------------------


@dataclass(frozen=True)
class DirPattern:
    text: str
    path: str
    regex: re.Pattern[str]
    depth: str  # "exact", "one" or "any"


@dataclass
class Query:
    text: str = ""
    words: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    counts: list[tuple[str, int]] = field(default_factory=list)
    dates: list[tuple[str, float, float]] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    dirs: list[DirPattern] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    @property
    def ranked(self) -> bool:
        """Whether the query has words, so the list is in BM25 order."""
        return bool(self.words or self.phrases)


def _midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _date_span(token: str, now: float) -> tuple[float, float] | None:
    today = _midnight(datetime.fromtimestamp(now))
    if token == "today":
        return today.timestamp(), (today + timedelta(days=1)).timestamp()
    if token == "yesterday":
        return (today - timedelta(days=1)).timestamp(), today.timestamp()
    if token == "week":
        return now - 7 * 86400, (today + timedelta(days=1)).timestamp()
    match = _DAY.fullmatch(token)
    if match is None:
        return None
    year, month, day = (int(part) if part else None for part in match.groups())
    try:
        if month is None:
            start, end = datetime(year, 1, 1), datetime(year + 1, 1, 1)
        elif day is None:
            start = datetime(year, month, 1)
            end = datetime(year + (month == 12), month % 12 + 1, 1)
        else:
            start = datetime(year, month, day)
            end = start + timedelta(days=1)
    except ValueError:
        return None
    return start.timestamp(), end.timestamp()


def _counts(token: str) -> list[tuple[str, int]] | None:
    tests = []
    for part in token.split(","):
        match = _COUNT.fullmatch(part)
        if match is None:
            return None
        tests.append((match.group(1), int(match.group(2))))
    return tests


def _component(part: str) -> str:
    """One path component of a shell glob as a regex that stays within it."""
    out = []
    index = 0
    while index < len(part):
        char = part[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[" and (close := part.find("]", index + 2)) > 0:
            body = part[index + 1:close]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            index = close
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def _absolute(text: str, *, home: str, cwd: str) -> str:
    if text == "~" or text.startswith("~/"):
        text = home + text[1:]
    elif not text.startswith("/"):
        text = cwd.rstrip("/") + "/" + text
    parts = [part for part in text.split("/") if part and part != "."]
    kept: list[str] = []
    for part in parts:
        if part == ".." and kept:
            kept.pop()
        elif part != "..":
            kept.append(part)
    return "/" + "/".join(kept)


def dir_pattern(text: str, *, home: str, cwd: str) -> DirPattern:
    """`dir:` as a shell glob over start directories: `*` and `?` stay within
    one path component, `**` spans zero or more of them."""
    path = _absolute(text, home=home, cwd=cwd)
    parts = [part for part in path.split("/") if part]
    regex = "".join("(?:/[^/]+)*" if part == "**" else "/" + _component(part) for part in parts)
    plain = parts and not _GLOB.search("".join(parts[:-1]))
    depth = "any" if plain and parts[-1] == "**" else "one" if plain and parts[-1] == "*" else "exact"
    try:
        compiled = re.compile(f"^{regex or '/'}$")
    except re.error:
        compiled = re.compile(r"(?!)")
    return DirPattern(text, path, compiled, depth)


def _tokens(text: str) -> list[tuple[str, bool]]:
    """Whitespace-separated tokens; a double-quoted run is one, marked quoted."""
    return [(match.group(1), True) if match.group(1) is not None else (match.group(2), False)
            for match in _TOKEN.finditer(text)]


def parse_query(text: str, *, now: float, home: str | Path, cwd: str | Path) -> Query:
    query = Query(text=text)
    home_text, cwd_text = str(home).rstrip("/") or "/", str(cwd).rstrip("/") or "/"
    pending = list(reversed(_tokens(text)))
    while pending:
        token, quoted = pending.pop()
        if quoted:
            if token.strip():
                query.phrases.append(" ".join(token.split()))
            continue
        head, colon, rest = token.partition(":")
        if colon and _date_span(head.lower(), now) is not None:
            if rest:
                pending.append((rest, False))
            token = head
        lowered = token.lower()
        if (span := _date_span(lowered, now)) is not None:
            query.dates.append((lowered, *span))
            continue
        if (tests := _counts(token)) is not None:
            query.counts.extend(tests)
            continue
        key, colon, rest = token.partition(":")
        key = key.lower()
        if colon and key in _FILTERS and rest:
            if key == "agent":
                query.agents.append(rest)
            elif key == "dir":
                query.dirs.append(dir_pattern(rest, home=home_text, cwd=cwd_text))
            elif key == "mode":
                query.modes.append("-p" if rest == "p" else rest.lower())
            else:
                query.models.append(rest)
            continue
        if any(char.isalnum() for char in token):
            query.words.append(token)
    return query


def describe(query: Query) -> str:
    """How the query was read, one clause per term."""
    parts: list[str] = []
    words = [*query.words, *(f'"{phrase}"' for phrase in query.phrases)]
    if words:
        parts.append(msgs.SESSIONS_Q_WORDS.text(words=" ".join(words)))
    if query.counts:
        parts.append(msgs.SESSIONS_Q_TURNS.text(test=" ".join(f"{op}{n}" for op, n in query.counts)))
    for label, _start, _end in query.dates:
        parts.append(msgs.SESSIONS_Q_DATE.text(date=label))
    parts += [msgs.SESSIONS_Q_AGENT.text(pattern=pattern) for pattern in query.agents]
    for pattern in query.dirs:
        base = pattern.text.rstrip("/").rsplit("/", 1)[0] if "/" in pattern.text else pattern.path
        if pattern.depth == "one":
            parts.append(msgs.SESSIONS_Q_DIR_ONE.text(path=base))
        elif pattern.depth == "any":
            parts.append(msgs.SESSIONS_Q_DIR_ANY.text(path=base))
        else:
            parts.append(msgs.SESSIONS_Q_DIR.text(pattern=pattern.text))
    parts += [msgs.SESSIONS_Q_MODE.text(mode=mode) for mode in query.modes]
    parts += [msgs.SESSIONS_Q_MODEL.text(pattern=pattern) for pattern in query.models]
    return " · ".join(parts) if parts else msgs.SESSIONS_Q_NOTHING.text()


def fts_expression(query: Query) -> str | None:
    """The FTS5 MATCH expression for the query's words: every word as a
    prefix, every phrase as it is, all required. None when there are none."""
    terms = [f'"{word.replace(chr(34), chr(34) * 2)}"*' for word in query.words]
    terms += [f'"{phrase.replace(chr(34), chr(34) * 2)}"' for phrase in query.phrases]
    return " AND ".join(terms) if terms else None


# --- filtering and order ---------------------------------------------------------


def _count_ok(value: int, op: str, n: int) -> bool:
    return {">": value > n, "<": value < n, ">=": value >= n, "<=": value <= n, "=": value == n}[op]


def matches(session: Session, query: Query) -> bool:
    """Whether the session passes every filter of the query. Words are not a
    filter here; search decides them."""
    if not all(_count_ok(session.turns, op, n) for op, n in query.counts):
        return False
    first = session.started if session.started is not None else session.when
    last = session.last if session.last is not None else first
    if not all(first < end and last >= start for _label, start, end in query.dates):
        return False
    if not all(session.agent is not None and fnmatchcase(session.agent, pattern) for pattern in query.agents):
        return False
    cwd = (session.cwd or "").rstrip("/") or ("/" if session.cwd else "")
    if not all(cwd and pattern.regex.match(cwd) for pattern in query.dirs):
        return False
    session_kind = kind(session)
    if not all(mode in (session.mode, session_kind) for mode in query.modes):
        return False
    return all(any(fnmatchcase(model.lower(), pattern.lower()) for model in session.models)
               for pattern in query.models)


def visible(session: Session, query: Query, *, show_all: bool) -> bool:
    """Shown sessions always; the hidden kinds with `a`, or when the query
    names their kind or mode."""
    session_kind = kind(session)
    if session_kind not in _settings.live_value("SESSION_HIDDEN_KINDS") or show_all:
        return True
    return any(mode in (session_kind, session.mode) for mode in query.modes)


def select(sessions: list[Session], query: Query, *, show_all: bool,
           scores: dict[str, float] | None = None) -> list[Session]:
    """The sessions the list shows, in order: best BM25 score first when the
    query has words (only sessions with a score), else the one last written
    to first, the newer start first between equals."""
    chosen = [session for session in sessions
              if visible(session, query, show_all=show_all) and matches(session, query)]
    if query.ranked:
        scores = scores or {}
        chosen = [session for session in chosen if session.path in scores]
        return sorted(chosen, key=lambda session: (scores[session.path], -session.active, -session.when))
    return sorted(chosen, key=lambda session: (-session.active, -session.when))


# --- the list's rows -------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One line of the list: a group heading, or a session at a nesting depth."""

    session: Session | None = None
    depth: int = 0
    last: bool = False
    group: str = ""


def short_dir(path: str | None, home: str | Path) -> str:
    if not path:
        return "-"
    home_text = str(home).rstrip("/")
    if home_text and (path == home_text or path.startswith(home_text + "/")):
        return "~" + path[len(home_text):]
    return path


def _nest(sessions: list[Session], nested: bool) -> list[Item]:
    if not nested:
        return [Item(session) for session in sessions]
    present = {session.path for session in sessions}
    children: dict[str, list[Session]] = {}
    for session in sessions:
        if session.branch_of in present and session.branch_of != session.path:
            children.setdefault(session.branch_of, []).append(session)
    items: list[Item] = []
    seen: set[str] = set()

    def walk(session: Session, depth: int, last: bool) -> None:
        if session.path in seen:
            return
        seen.add(session.path)
        items.append(Item(session, depth, last))
        kids = sorted(children.get(session.path, []), key=lambda child: child.when)
        for index, child in enumerate(kids):
            walk(child, depth + 1, index == len(kids) - 1)

    for session in sessions:
        if session.branch_of not in present or session.branch_of == session.path:
            walk(session, 0, False)
    for session in sessions:  # a branch cycle has no root; list what is left flat
        walk(session, 0, False)
    return items


def build_items(sessions: list[Session], view: str, *, home: str | Path, nested: bool = True) -> list[Item]:
    """The list's lines for a view: flat, or grouped by start directory or by
    agent, groups in the order their first session appears. Branches sit under
    the session they came from when `nested`."""
    if view == "flat":
        return _nest(sessions, nested)
    groups: dict[str, list[Session]] = {}
    for session in sessions:
        key = short_dir(session.cwd, home) if view == "dir" else (session.agent or "-")
        groups.setdefault(key, []).append(session)
    items: list[Item] = []
    for key, members in groups.items():
        items.append(Item(group=f"[{key}]"))
        items.extend(_nest(members, nested))
    return items


def view_name(view: str) -> str:
    return _VIEW_NAMES[view].text()


def when_text(ts: float | None, now: float) -> str:
    if ts is None:
        return "-"
    moment = datetime.fromtimestamp(ts)
    if moment.year == datetime.fromtimestamp(now).year:
        return moment.strftime("%b %d %H:%M")
    return moment.strftime("%b %d %Y")


def clock_text(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts is not None else "--:--:--"


def length_text(session: Session) -> str:
    if session.started is None or session.last is None:
        return "-"
    seconds = max(0, int(session.last - session.started))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _fit(text: str, width: int) -> str:
    text = " ".join(text.split())
    if len(text) > width:
        text = text[:max(0, width - 1)] + "…"
    return text.ljust(width)


def kind_text(session: Session) -> str:
    session_kind = kind(session)
    return "-" if session_kind == SHOWN else session_kind


def name_kind_text(session: Session) -> str:
    """The row's last column: the session's name when it has one, then its
    kind when it is a hidden one."""
    marked = kind_text(session)
    if session.name is None:
        return marked
    return session.name if marked == "-" else f"{session.name} · {marked}"


_WHEN, _MODE, _AGENT, _DIR, _TURNS, _LENGTH = 12, 4, 16, 28, 5, 6


def header_line() -> str:
    return (f"  {'when':<{_WHEN}}  {'mode':<{_MODE}}  {'agent':<{_AGENT}}  {'dir':<{_DIR}} "
            f"{'turns':>{_TURNS}}  {'length':<{_LENGTH}} name")


def session_line(item: Item, *, now: float, home: str | Path, marker: str = " ") -> str:
    """A session row: marker, when, mode, agent, dir, turns, length, then the
    session's name when it has one and its kind. A nested branch shows its
    branch point in place of mode, agent and dir."""
    session = item.session
    assert session is not None
    tail = (f" {session.turns:>{_TURNS}}  {length_text(session):<{_LENGTH}} {name_kind_text(session)}")
    when = when_text(session.started if session.started is not None else session.when, now)
    if item.depth == 0:
        return (f"{marker} {when:<{_WHEN}}  {_fit(session.mode or '-', _MODE)}  "
                f"{_fit(session.agent or '-', _AGENT)}  {_fit(short_dir(session.cwd, home), _DIR)}{tail}")
    prefix = "   " * (item.depth - 1) + ("└─ " if item.last else "├─ ")
    point = msgs.SESSIONS_BRANCH.text(point=session_store.point_label(session.branch_point)
                                      if session.branch_point is not None else "-")
    span = _MODE + _AGENT + _DIR + 4 - len(prefix)
    return f"{marker} {prefix}{when:<{_WHEN}}  {_fit(point, span)}{tail}"


def excerpt(text: str, spans: tuple[tuple[int, int], ...], *, lead: int = 30,
            width: int = 160) -> tuple[str, tuple[tuple[int, int], ...]]:
    """The part of a matched row to show: from a little before the first
    match, at most `width` characters, with the match spans moved to fit."""
    start = max(0, spans[0][0] - lead) if spans else 0
    head = "…" if start else ""
    cut = text[start:start + width]
    tail = "…" if start + width < len(text) else ""
    shift = start - len(head)
    moved = tuple((max(0, a - shift), min(len(head) + len(cut), b - shift))
                  for a, b in spans if b - shift > len(head) and a - shift < len(head) + len(cut))
    return head + cut + tail, moved


def hit_line(number: int | None, who: str, text: str) -> str:
    """A search hit's matching row, as it shows under the session."""
    head = f"#{number:04d} " if isinstance(number, int) and number > 0 else ""
    return f"{head}{who}  {' '.join(text.split())}"


def message_lines(rows: list[Any]) -> list[str]:
    """The message list of a session: number, clock, who and heading per row,
    and the model stamp on the rows where it changes."""
    lines = []
    model = None
    for row in rows:
        text = f"#{row.number:04d} {clock_text(row.ts)} {row.who:<10} | {' '.join(row.heading().split())}"
        if row.model and row.model != model:
            model = row.model
            text += f"  [{model}]"
        lines.append(text)
    return lines



# The agent's status line begins with 📌 (the 📌🔨🦍 group): what the preview
# shows for it.
_HARNESS_OPEN = tuple(f"<{tag}>" for tag in HARNESS_USER_TAGS)
_HARNESS_CLOSE = tuple(f"</{tag}>" for tag in HARNESS_USER_TAGS)


def _operator_lines(lines: list[str]) -> list[str]:
    """A user message's lines without the blocks js wrote into it."""
    kept, inside = [], False
    for line in lines:
        text = line.strip()
        if text.startswith(_HARNESS_OPEN):
            inside = not text.endswith(_HARNESS_CLOSE)
            continue
        if inside:
            inside = not text.endswith(_HARNESS_CLOSE)
            continue
        kept.append(line)
    return kept


def _message_lines(message: Any) -> list[str]:
    """A message's text, one entry per non-blank line."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "\n".join(part.get("text", "") for part in content
                            if isinstance(part, dict) and isinstance(part.get("text"), str))
    if not isinstance(content, str):
        return []
    return [line.rstrip() for line in content.splitlines() if line.strip()]


def preview(messages: list[Any]) -> tuple[str, str]:
    """What the picker shows under the highlighted session: the operator's
    last text, and the agent's last status line (one that begins with 📌),
    else the last line of its last text. The agent's text includes what it
    wrote beside tool calls, answered or not. Each on one line; "" when there is none."""
    user = ""
    said: list[list[str]] = []
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else None
        if role == "user":
            kept = _operator_lines(_message_lines(message))
            if kept:
                user = " ".join(" ".join(kept).split())
        elif role == "assistant":
            lines = _message_lines(message)
            if lines:
                said.append(lines)
    pinned = next((line for lines in reversed(said) for line in reversed(lines)
                   if line.lstrip().startswith(_settings.live_value("SESSION_STATUS_MARK"))), None)
    agent = pinned if pinned is not None else (said[-1][-1] if said else "")
    return user, " ".join(agent.split())
