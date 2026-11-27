"""Text laid out in terminal columns: how many cells a text takes
(`cells`), where a line breaks into rows (`cut_points`), text and coloured
runs wrapped to a width, and the width of the terminal itself."""

from __future__ import annotations

import shutil

from prompt_toolkit.utils import get_cwidth

from . import fset

# What `field_rows` breaks a line at: put before each field in
# place of its separator before the line is rendered.
FIELD_BREAK = "\x00"


def _slice_runs(parts: list[fset.Run], start: int, end: int) -> list[fset.Run]:
    """The runs covering characters ``start``..``end`` of their plain text."""
    out: list[fset.Run] = []
    position = 0
    for colour, text in parts:
        low, high = max(start - position, 0), min(end - position, len(text))
        if low < high:
            out.append((colour, text[low:high]))
        position += len(text)
    return out


def field_rows(parts: list[fset.Run], width: int) -> list[list[fset.Run]]:
    """Runs whose fields each follow a `FIELD_BREAK` as rows of runs at most
    ``width`` columns wide, broken between two fields; the rows after the
    first start under the first field. A field wider than its row breaks at
    the column and goes on under the first field. A line with no field is
    word-wrapped. Wrapping works on the plain text; the colours ride along."""
    parts = [(colour, text.expandtabs(TAB)) for colour, text in parts]
    plain = fset.to_plain(parts)
    pieces = plain.split(FIELD_BREAK)
    spans: list[tuple[int, int]] = []
    position = 0
    for piece in pieces:
        spans.append((position, position + len(piece)))
        position += len(piece) + len(FIELD_BREAK)
    if len(pieces) == 1:
        rows, start = [], 0
        for index, end in enumerate(cut_points(plain, width)):
            if index:
                # The spaces at a break are the break: they start no row.
                start = end - len(plain[start:end].lstrip(" "))
                if start >= end:
                    continue
            rows.append(_slice_runs(parts, start, len(plain[:end].rstrip(" ")) if end < len(plain) else end))
            start = end
        return rows
    head_low, head_high = spans[0]
    head = plain[head_low:head_high]
    rows: list[list[fset.Run]] = []
    start = 0
    head_cuts = cut_points(head, width) if head else [0]
    for end in head_cuts:
        # The text before the first field wraps like any line.
        rows.append(_slice_runs(parts, head_low + start, head_low + end))
        start = end
    used = cells(head[head_cuts[-2]:] if len(head_cuts) > 1 else head)
    lead = used if used < width // 2 else 0
    for index, (low, high) in enumerate(spans[1:]):
        field = plain[low:high]
        size = cells(field)
        gap = 2 if index else 0
        if used + gap + size > width and used > lead:
            rows.append([("", " " * lead)] if lead else [])
            used, gap = lead, 0
        room = width - used - gap
        if gap:
            rows[-1].append(("", " " * gap))
        if size <= room:
            rows[-1] += _slice_runs(parts, low, high)
            used += gap + size
            continue
        # Wider than what is left of the row: it breaks at the column.
        start = 0
        cuts = cut_points(field, max(room, 1), max(width - lead, 1), words=False)
        for number, end in enumerate(cuts):
            if number:
                rows.append([("", " " * lead)] if lead else [])
            rows[-1] += _slice_runs(parts, low + start, low + end)
            start = end
        used = (lead if len(cuts) > 1 else used + gap) + cells(field[cuts[-2] if len(cuts) > 1 else 0:])
    return rows


def drawn_rows(parts: list[fset.Run], width: int, base: str = "") -> list[str]:
    """Runs word-wrapped to ``width`` columns (`field_rows`), each row as
    drawn text; a run with no colour of its own in colour entry ``base``."""
    colour = fset.hex_for(base) if base else None
    parts = [(own or colour or "", text) for own, text in parts]
    return [fset.to_ansi(row) for row in field_rows(parts, width)]


def hanging_rows(parts: list[fset.Run], width: int, indent: int) -> list[str]:
    """Coloured runs word-wrapped with subsequent rows under ``indent`` cells."""
    width = max(2, width)
    indent = indent if 0 <= indent <= width - 2 else min(4, width // 2, width - 2)
    rows: list[str] = []
    for number, line in enumerate(fset.split_lines([(colour, text.expandtabs(TAB)) for colour, text in parts])):
        plain = fset.to_plain(line)
        start = 0
        room = width - indent if number else width
        for index, end in enumerate(cut_points(plain, room, width - indent)):
            low = start
            if index:
                low += len(plain[start:end]) - len(plain[start:end].lstrip(" "))
            high = end if end == len(plain) else len(plain[:end].rstrip(" "))
            prefix = " " * indent if number or index else ""
            rows.append(prefix + fset.to_ansi(_slice_runs(line, low, high)))
            start = end
    return rows


# Columns between tab stops.
TAB = 8


def cells(text: str) -> int:
    """Terminal columns ``text`` takes on one line from column 0: colour
    escapes not counted, a tab out to the next stop, a wide character two.
    Every measure in js goes through here."""
    if text.isascii() and text.isprintable():
        return len(text)
    return sum(get_cwidth(char) for char in fset.plain(text).expandtabs(TAB))


def cut_points(plain: str, room: int, rest: int | None = None, *, words: bool = True) -> list[int]:
    """Where ``plain`` (one line, no newline) breaks into rows: the end of
    each row, the last one ``len(plain)``. The first row holds ``room``
    columns, the rest ``rest`` (``room`` by default). With ``words`` a row
    ends after its last space that fits, else at the column; a row always
    holds at least one character. One pass, with running column counts.
    Every wrapping in js goes through here."""
    rest = room if rest is None else rest
    if len(plain) <= room and plain.isascii() and plain.isprintable():
        return [len(plain)]
    cuts: list[int] = []
    start, used, space, width = 0, 0, -1, room
    widths = [get_cwidth(char) for char in plain]
    for index, cell in enumerate(widths):
        if used + cell > width and index > start:
            end = space + 1 if words and space >= start else index
            cuts.append(end)
            start, space, width, used = end, -1, rest, 0
            # The text carried onto the new row holds no space; cut it again
            # wherever it is wider than that row.
            for carried in range(end, index):
                if used + widths[carried] > width and carried > start:
                    cuts.append(carried)
                    start, used = carried, 0
                used += widths[carried]
            if used + cell > width and index > start:
                cuts.append(index)
                start, used = index, 0
        if plain[index] == " ":
            space = index
        used += cell
    cuts.append(len(plain))
    return cuts


def wrap_words(text: str, width: int) -> str:
    """``text`` with each line wider than ``width`` columns broken at the
    last space that fits, or at the column when no space does. The spaces at
    a break are the break: they end no row and start none."""
    out: list[str] = []
    for line in text.expandtabs(TAB).split("\n"):
        start = 0
        cuts = cut_points(line, width)
        for index, end in enumerate(cuts):
            row = line[start:end]
            start = end
            if index:
                row = row.lstrip(" ")
                if not row:
                    continue
            out.append(row.rstrip(" ") if index < len(cuts) - 1 else row)
    return "\n".join(out)


def terminal_width() -> int:
    return max(20, shutil.get_terminal_size((80, 24)).columns)


def block_width() -> int:
    """Columns a drawn block may fill: one short of the terminal. The async
    screen's scrollback keeps a cursor cell after every line, so a row as wide
    as the window wraps onto a blank second row."""
    return terminal_width() - 1
