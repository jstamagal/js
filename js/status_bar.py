"""The REPL's status bar: its styles, the line it draws (model, context, throbber, clock, session), and the tick that repaints it."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.styles import Style

from . import fset, messages as msgs, session_store, text_layout
from .toolkit.tool_suites import display_name

TICK_BUSY_S = 0.1
TICK_IDLE_S = 1.0


def status_style() -> str:
    """The bar's prompt_toolkit style: COLOR_STATUS_TEXT on COLOR_STATUS_BACKGROUND."""
    if not fset.colour_on():
        return "bold reverse"
    fg = fset.hex_for("COLOR_STATUS_TEXT")
    bg = fset.hex_for("COLOR_STATUS_BACKGROUND")
    return " ".join(filter(None, ("bold", fg, f"bg:{bg}" if bg else "")))


def screen_style() -> str:
    """The whole screen's prompt_toolkit style: the terminal's own text, on
    COLOR_BACKGROUND when it is set, else on the terminal's own."""
    if not fset.colour_on():
        return ""
    bg = fset.hex_for("COLOR_BACKGROUND")
    return f"bg:{bg}" if bg else ""


def accent_clock(line: str) -> str:
    """The bar's `[HH:MM]` lead in COLOR_STATUS_ACCENT, as ANSI text."""
    end = line.find("]") + 1 if line.startswith("[") else 0
    if not end:
        return line
    code = fset.fg("COLOR_STATUS_ACCENT")
    return f"{code}{line[:end]}{fset.FG_DEFAULT}{line[end:]}" if code else line


@functools.lru_cache(maxsize=8)
def status_style_sheet(colours: str) -> Style:
    return Style.from_dict({"status": colours})


def format_count(n: int) -> str:
    """Output tokens as a heartbeat: hundreds under 10k, then one-decimal k."""
    if n < 10_000:
        return f"{n // 100 * 100:,}"
    return f"{n / 1000:.1f}k"


def throbber_frame(now: float) -> str:
    frames = fset.setting_value("SPINNER_FRAMES")
    return frames[int(now / TICK_BUSY_S) % len(frames)] if frames else ""


def turn_centre(status, *, now: float) -> tuple[str, int | None]:
    """(phase, output_tokens) for the bar's centre group from a TurnStatus.

    A retry's wait wins, then a running tool, then compaction, then the wait
    for a request's first byte once it passes a second, then the
    output-token count.
    """
    if status.retry:
        left = max(0.0, status.retry_at - now)
        return msgs.STATUS_RETRY.words(n=status.retry, of=status.retry_of, seconds=left), None
    if status.tool:
        extra = msgs.STATUS_TOOL_EXTRA.words(tool_extra=status.tool_extra) if status.tool_extra else ""
        elapsed = int(max(0.0, now - status.tool_started))
        return msgs.STATUS_TOOL_ELAPSED.words(tool=display_name(status.tool), extra=extra, elapsed=elapsed), None
    if status.compacting:
        return _field(msgs.STATUS_COMPACTING), None
    if status.waiting_since and now - status.waiting_since >= 1:
        return msgs.STATUS_FIRST_BYTE_WAIT.words(waited=msgs.duration(now - status.waiting_since)), None
    return "", status.output_tokens or None


def _field(message: msgs.Message, **fields) -> str:
    """One status bar field through its /fset format, palette colours kept;
    a tab is one space on the bar's one row."""
    return message.line(**fields).replace("\t", " ")


def _width(text: str) -> int:
    """Columns ``text`` takes on the bar (`js.text_layout.cells`)."""
    return text_layout.cells(text)


def status_line(
    width: int,
    *,
    clock: str,
    provider: str | None,
    model: str | None,
    context_tokens: int | None,
    phase: str,
    output_tokens: int | None,
    throbber: str,
    agent_id: str | None,
    session_short: str | None,
    cache_pct: int | None,
    cost: str | None = None,
    window: int | None = None,
    activity: list[int] | None = None,
) -> str:
    """The status bar as a plain string of exactly `width` cells.

    ``window`` is the number of the window on screen, shown after the clock
    while more than one is open; ``activity`` the windows with output not yet
    seen, shown ircII's way, `[Act: 3,5]`, at the head of the right group.

    Left `[HH:MM] provider/model context`, centre `throbber phase count` (only
    while `throbber` is set, i.e. a turn runs), right `agent/session cache N%
    cost`, where `cost` is the session's spend (`usage_display.status_text`). Each
    field is drawn through its /fset format, STATUS_CLOCK and the rest.
    Groups that do not fit give way in a fixed order: cache, then the model's
    head, then the provider, then the centre count, then the cost, then the
    agent id. The session name stays, elided with an ellipsis when it still
    does not fit.
    """
    if width <= 0:
        return ""
    model_text = model or ""
    session_text = session_short or ""
    show = {"cache": cache_pct is not None, "provider": bool(provider),
            "count": output_tokens is not None, "agent": bool(agent_id), "cost": bool(cost)}

    clock_text = _field(msgs.STATUS_CLOCK, clock=clock)
    if window is not None:
        clock_text += " " + _field(msgs.STATUS_WINDOW, number=window)
    activity_text = _field(msgs.STATUS_ACTIVITY, numbers=",".join(map(str, activity))) if activity else ""

    def groups() -> tuple[str, str, str]:
        route = "/".join(filter(None, (provider if show["provider"] else "", model_text)))
        left = " ".join(filter(None, (
            clock_text, _field(msgs.STATUS_ROUTE, route=route) if route else "",
            "" if context_tokens is None else _field(msgs.STATUS_CONTEXT, tokens=f"{context_tokens:,}"))))
        centre = ""
        if throbber:
            count = (_field(msgs.STATUS_REPLY, tokens=format_count(output_tokens))
                     if show["count"] and output_tokens is not None else "")
            centre = " ".join(filter(None, (_field(msgs.STATUS_SPINNER, spinner=throbber),
                                            _field(msgs.STATUS_PHASE, phase=phase) if phase else "", count)))
        who = "/".join(filter(None, (agent_id if show["agent"] else "", session_text)))
        right = " ".join(filter(None, (activity_text, _field(msgs.STATUS_WHO, who=who) if who else "",
                                       _field(msgs.STATUS_CACHE, pct=cache_pct) if show["cache"] else "",
                                       _field(msgs.STATUS_COST, cost=cost) if show["cost"] else "")))
        return left, centre, right

    def fits(left: str, centre: str, right: str) -> bool:
        gaps = 4 if centre else (2 if right else 0)
        return _width(left) + _width(centre) + _width(right) + gaps <= width

    def truncate_model() -> None:
        nonlocal model_text
        if len(model_text) > 12:
            model_text = "…" + model_text[-12:]

    steps = (
        lambda: show.update(cache=False),
        truncate_model,
        lambda: show.update(provider=False),
        lambda: show.update(count=False),
        lambda: show.update(cost=False),
        lambda: show.update(agent=False),
    )
    left, centre, right = groups()
    for step in steps:
        if fits(left, centre, right):
            break
        step()
        left, centre, right = groups()

    gap = width - _width(left) - _width(right)
    if centre and gap >= _width(centre) + 4:
        before = max(2, (gap - _width(centre)) // 2)
        middle = " " * before + centre
        line = left + middle + " " * (gap - _width(middle)) + right
    elif not centre and gap >= (2 if right else 0):
        line = left + " " * gap + right
    else:
        # Still too wide: keep the clock, throbber and session, cut the left group.
        room = width - _width(clock_text) - 1 - (_width(centre) + 1 if centre else 0)
        session_text = _elide(session_text, room)
        left, centre, right = (fset.plain(group) for group in groups())
        tail = " ".join(filter(None, (centre, right)))
        room = max(_width(clock_text), width - _width(tail) - 1)
        line = " ".join(filter(None, (_cut(left, room), tail)))
    if _width(line) > width:
        line = _cut(fset.plain(line), width)
    return line + " " * (width - _width(line))


def session_short(session_file) -> str:
    """The session as the bar names it: the first 8 hex chars of a generated
    `<timestamp>-<hex>` stem, else the whole stem, the name it was given."""
    stem = getattr(session_file, "stem", "") or ""
    return stem.rsplit("-", 1)[-1][:8] if session_store.is_generated(stem) else stem


def _cut(text: str, room: int) -> str:
    """The head of plain ``text`` that fits in ``room`` columns."""
    return text[:text_layout.cut_points(text, max(room, 0), words=False)[0]] if room > 0 else ""


def _elide(text: str, room: int) -> str:
    """`text` in at most `room` cells, cut short with an ellipsis."""
    if _width(text) <= room:
        return text
    return _cut(text, room - 1) + "…" if room > 1 else ""


async def tick(app: Application, busy: Callable[[], bool]) -> None:
    """Repaint the bar: every 0.1s while `busy()`, else once a second for the clock."""
    while True:
        app.invalidate()
        await asyncio.sleep(TICK_BUSY_S if busy() else TICK_IDLE_S)
