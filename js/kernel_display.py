"""Console rendering for kernel execution and activity events."""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import fset as _fset
from . import messages as msgs
from . import settings as _settings
from .toolkit.sanitize import int_or_default


def render_max_lines(context: Any) -> int:
    value = int_or_default(getattr(context, "kernel_render_max_lines", None),
                           _settings.default_value("KERNEL_LINES_MAX"), minimum=1)
    return value


def _style(role: str) -> str:
    """Rich style for /fset colour entry ``role``; "" when it has none."""
    return _fset.hex_for(role) or ""


def _piece(runs: list, style: str = "") -> Any:
    """A message's colour runs (`Message.body_runs`) as rich text on ``style``."""
    return _fset.to_rich(runs, style)


def _console() -> Any:
    """Where the kernel panel draws: stderr, in the colour it takes
    (`messages.color_system`), one column short of the window as every
    block js draws (`display.block_width`)."""
    from rich.console import Console

    from .display import block_width

    system = msgs.color_system(sys.stderr)
    return Console(file=sys.stderr, width=block_width(), soft_wrap=False, highlight=False, color_system=system,
                   force_terminal=system is not None, no_color=system is None)


def _clip(text: str, limit: int | None) -> tuple[str, int]:
    lines = text.splitlines()
    if limit is None or len(lines) <= limit:
        return text, 0
    return "\n".join(lines[:limit]), len(lines) - limit


def _drawn(draw: Callable[[Any, int | None], None], limit: int | None) -> str:
    """What ``draw`` prints with sections capped at ``limit`` lines, as text
    for stderr."""
    from rich.console import Console

    from .display import block_width

    system = msgs.color_system(sys.stderr)
    console = Console(file=io.StringIO(), width=block_width(), soft_wrap=False, highlight=False,
                      color_system=system, force_terminal=system is not None, no_color=system is None)
    draw(console, limit)
    return console.file.getvalue()


def _show(draw: Callable[[Any, int | None], None], limit: int) -> None:
    """Print a kernel render. KERNEL_LINES_MAX caps it only on the screen,
    as a fold the reasoning_toggle key unfolds to the whole render; anywhere
    else nothing can unfold it, so it prints whole."""
    folded = getattr(sys.stderr, "write_folded", None)
    if callable(folded):
        capped, whole = _drawn(draw, limit), _drawn(draw, None)
        if capped != whole:
            folded(capped, whole)
            return
    draw(_console(), None)


def render_execution(
    context: Any,
    *,
    level: str,
    code: str,
    stdout: str,
    stderr: str,
    display: str,
    error: str,
    elapsed: float,
    cell: int,
    added: list[str],
    removed: list[str],
    namespace: list[str],
    images: list[Path],
    interrupted: bool,
) -> None:
    if level == "quiet" and not error and not interrupted:
        return
    from .display import clean

    code, stdout, stderr, display, error = (clean(part) for part in (code, stdout, stderr, display, error))
    _show(lambda console, limit: _draw_execution(
        console, limit, level=level, code=code, stdout=stdout, stderr=stderr, display=display, error=error,
        elapsed=elapsed, cell=cell, added=added, removed=removed, namespace=namespace, images=images,
        interrupted=interrupted), render_max_lines(context))


def _draw_execution(console: Any, limit: int | None, *, level: str, code: str, stdout: str, stderr: str,
                    display: str, error: str, elapsed: float, cell: int, added: list[str], removed: list[str],
                    namespace: list[str], images: list[Path], interrupted: bool) -> None:
    """One kernel call's render on ``console``, each section capped at
    ``limit`` lines (None for whole)."""
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text

    dim = _style("COLOR_DIM")

    if level == "quiet":
        # Severity is the colour: warn for an interrupt, error for a failure.
        style = "bold " + _style("COLOR_WARN" if interrupted and not error else "COLOR_ERROR")
        body, hidden = _clip(error, limit) if error else ("", 0)
        said = Text(" " + body) if error else Text(" ") + _piece(msgs.KERNEL_CELL_INTERRUPTED.body_runs())
        console.print(_piece(msgs.KERNEL_CELL.body_runs(cell=cell), style=style) + said)
        if hidden:
            console.print(Text("  ") + _piece(msgs.KERNEL_MORE_LINES.body_runs(count=hidden), style=dim))
        return

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, style="bold " + _style("COLOR_ACCENT"))
    grid.add_column(overflow="fold")

    shown_code, code_hidden = _clip(code.strip(), limit)
    grid.add_row("code", Syntax(shown_code, "python", theme=_fset.code_style(),
                                word_wrap=True, background_color="default"))
    if code_hidden:
        grid.add_row("", _piece(msgs.KERNEL_MORE_CODE_LINES.body_runs(count=code_hidden), style=dim))

    if level == "verbose":
        sections = (("stdout", stdout, ""), ("stderr", stderr, _style("COLOR_WARN")),
                    ("display", display, _style("COLOR_ACCENT")))
    else:
        merged = "".join(part for part in (stdout, stderr, display) if part)
        sections = (("out", merged, ""),)

    for label, body, style in sections:
        if not body.strip():
            continue
        shown, hidden = _clip(body.rstrip("\n"), limit)
        grid.add_row(label, Text(shown, style=style or None))
        if hidden:
            grid.add_row("", _piece(msgs.KERNEL_MORE_LINES.body_runs(count=hidden),
                                  style=dim))

    if error:
        shown, hidden = _clip(error.rstrip("\n"), limit)
        grid.add_row("error", Text(shown, style=_style("COLOR_ERROR")))
        if hidden:
            grid.add_row("", _piece(msgs.KERNEL_MORE_LINES.body_runs(count=hidden),
                                  style=dim))
    if interrupted:
        grid.add_row("stopped", _piece(msgs.KERNEL_STOPPED.body_runs(), style="bold " + _style("COLOR_WARN")))
    if added:
        grid.add_row("defined", Text(", ".join(added), style=_style("COLOR_OK")))
    if removed:
        grid.add_row("gone", Text(", ".join(removed), style=_style("COLOR_ERROR")))
    grid.add_row("namespace", Text(", ".join(namespace)) if namespace else _piece(msgs.NONE_VALUE.body_runs()))
    for image in images:
        grid.add_row("image", Text(str(image), style=_style("COLOR_ACCENT")))

    console.print(Panel(grid, title=_piece(msgs.KERNEL_PANEL_TITLE.body_runs(cell=cell)),
                        subtitle=f"{elapsed:.2f}s", title_align="left",
                        subtitle_align="right", border_style=_style("COLOR_SEPARATOR")))


def render_event(context: Any, level: str, message: str, *, style: str = "COLOR_ACCENT",
                 verbose_only: bool = False) -> None:
    """Lifecycle/activity note made of text: kernel died, tool saved, a
    toolbox listing. The text is cleaned; see `render_note`."""
    from .display import clean

    lines = [[("", line)] for line in clean(message).splitlines()]
    render_note(context, level, lines, style=style, verbose_only=verbose_only)


def render_entry(context: Any, level: str, runs: list, *, style: str = "COLOR_ACCENT",
                 verbose_only: bool = False) -> None:
    """Lifecycle note from an entry's colour runs (`Message.body_runs`), drawn
    as its format says: kernel started, restarted."""
    render_note(context, level, _fset.split_lines(runs), style=style, verbose_only=verbose_only)


def render_note(context: Any, level: str, lines: list, *, style: str = "COLOR_ACCENT",
                verbose_only: bool = False) -> None:
    """Lines of colour runs as `· ` notes in ``style``, the /fset colour entry
    a run without its own colour is drawn in.

    Multi-line messages print one line per line. Folding them onto one line with
    separators is how a five-tool toolbox listing became an unreadable ribbon.
    """
    if level == "quiet":
        return
    if verbose_only and level != "verbose":
        return
    from rich.text import Text

    dim = _style("COLOR_DIM")
    lines = lines or [[]]

    def draw(console: Any, limit: int | None) -> None:
        shown = lines if limit is None else lines[:limit]
        for runs in shown:
            console.print(Text("· ", style=dim) + _piece(runs, _style(style)))
        if len(shown) < len(lines):
            console.print(Text("  ") + _piece(msgs.KERNEL_EVENT_MORE_LINES.body_runs(count=len(lines) - len(shown)),
                                              style=dim))

    _show(draw, render_max_lines(context))

