"""Persistent PTY sessions and terminal screen images."""

from __future__ import annotations

import atexit
import json
import os
import re
import signal
import time
from pathlib import Path
from typing import Any

from . import fs
from .core import Tool, ToolContext
from .descriptions import load_description
from .sanitize import int_or_default, text_or_default

DEFAULT_COLS = 64
DEFAULT_ROWS = 36

_KEY_SEQUENCES = {
    "enter": "\r",
    "return": "\r",
    "tab": "\t",
    "space": " ",
    "esc": "\x1b",
    "escape": "\x1b",
    "backspace": "\x7f",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pgup": "\x1b[5~",
    "pgdn": "\x1b[6~",
    "insert": "\x1b[2~",
    "delete": "\x1b[3~",
    "ctrl-c": "\x03",
    "ctrl-d": "\x04",
    "ctrl-l": "\x0c",
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "f9": "\x1b[20~",
    "f10": "\x1b[21~",
    "f11": "\x1b[23~",
    "f12": "\x1b[24~",
}

_FONT_CANDIDATES = (
    "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf",
    "/usr/share/fonts/TTF/GeistMono[wght].ttf",
    "/usr/share/fonts/roboto/RobotoMono-Light.otf",
    "/usr/share/fonts/TTF/VictorMono[wght].ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)

_ANSI_RGB = {
    "black": (0, 0, 0),
    "red": (205, 49, 49),
    "green": (13, 188, 121),
    "brown": (229, 229, 16),
    "yellow": (229, 229, 16),
    "blue": (36, 114, 200),
    "magenta": (188, 63, 188),
    "cyan": (17, 168, 205),
    "white": (229, 229, 229),
    "brightblack": (102, 102, 102),
    "brightred": (241, 76, 76),
    "brightgreen": (35, 209, 139),
    "brightyellow": (245, 245, 67),
    "brightblue": (59, 142, 234),
    "brightmagenta": (214, 112, 214),
    "brightcyan": (41, 184, 219),
    "brightwhite": (255, 255, 255),
}

_BACKGROUND = (12, 12, 12)
_FOREGROUND = (222, 222, 222)
_LIVE_CHILDREN: set[Any] = set()


def _colour(name: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    if not name or name == "default":
        return fallback
    named = _ANSI_RGB.get(name)
    if named is not None:
        return named
    if re.fullmatch(r"[0-9a-fA-F]{6}", name):
        return tuple(int(name[index:index + 2], 16) for index in (0, 2, 4))
    return fallback


def _render_lines(screen: Any) -> list[str]:
    return [line.rstrip() for line in screen.display]


def _report(payload: dict[str, Any], context: ToolContext) -> str:
    text = json.dumps(payload, indent=2)
    if len(text.encode("utf-8")) <= context.max_tool_result_bytes:
        return text
    return text.encode("utf-8")[: context.max_tool_result_bytes].decode(
        "utf-8", errors="ignore"
    ) + "\n[truncated]"


def _drain(state: dict[str, Any], wait_ms: int) -> None:
    import pexpect

    deadline = time.monotonic() + max(0, wait_ms) / 1000.0
    child = state["child"]
    first_read = True
    while first_read or time.monotonic() < deadline:
        first_read = False
        timeout = min(0.1, max(0.0, deadline - time.monotonic()))
        try:
            chunk = child.read_nonblocking(size=65536, timeout=timeout)
        except pexpect.TIMEOUT:
            continue
        except (pexpect.EOF, OSError):
            break
        if chunk:
            state["stream"].feed(chunk.decode("utf-8", errors="replace"))


def _observe(
    state: dict[str, Any], session: str, did: str, wait_ms: int, context: ToolContext
) -> str:
    _drain(state, wait_ms)
    child = state["child"]
    screen = state["screen"]
    lines = _render_lines(screen)
    payload: dict[str, Any] = {
        "session": session,
        "did": did,
        "lines": lines,
        "nonblank_lines": sum(1 for line in lines if line.strip()),
        "cursor": [screen.cursor.y, screen.cursor.x],
        "still_running": child.isalive(),
    }
    previous = state.get("previous_lines")
    if previous is not None:
        changed = sum(1 for before, after in zip(previous, lines, strict=False) if before != after)
        payload["lines_changed"] = changed
        payload["screen_responded"] = changed > 0
    state["previous_lines"] = lines

    if not child.isalive():
        _LIVE_CHILDREN.discard(child)
        try:
            child.wait()
        except Exception:
            pass
        payload["exit_status"] = child.exitstatus
        payload["signal_status"] = child.signalstatus
    payload["reading"] = (
        "lines is the rendered terminal screen. nonblank_lines=0 means the user sees "
        "nothing. lines_changed and screen_responded compare this screen with the "
        "most recent observation: for send they describe change after the sent keys; "
        "for look they describe passive change since the prior observation. "
        "terminal_snapshot updates that comparison baseline before a later send. "
        "still_running is normal for a TUI and suspicious for a one-shot command. "
        "Use terminal_snapshot to inspect layout and colour."
    )
    return _report(payload, context)


def _stop_state(state: dict[str, Any]) -> None:
    child = state["child"]
    try:
        if child.isalive():
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except OSError:
                child.kill(signal.SIGTERM)
            time.sleep(0.15)
        if child.isalive():
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                child.kill(signal.SIGKILL)
        child.close(force=True)
    except Exception:
        pass
    finally:
        _LIVE_CHILDREN.discard(child)


def close_terminal_sessions(context: ToolContext) -> None:
    for state in list(context.terminal_sessions.values()):
        _stop_state(state)
    context.terminal_sessions.clear()


def _cleanup_live_children() -> None:
    for child in list(_LIVE_CHILDREN):
        try:
            if child.isalive():
                child.kill(signal.SIGTERM)
            child.close(force=True)
        except Exception:
            pass
        finally:
            _LIVE_CHILDREN.discard(child)


atexit.register(_cleanup_live_children)


def terminal_session(
    action: str,
    session: str | None = "main",
    command: str | None = "",
    keys: str | None = "",
    cwd: str | None = "",
    wait_ms: int | None = 700,
    cols: int | None = None,
    rows: int | None = None,
    context: ToolContext | None = None,
) -> str:
    """Start, drive, inspect, stop, or list a persistent PTY session."""
    if context is None:
        return "ERROR: missing ToolContext"
    try:
        import pexpect
        import pyte
    except ImportError as exc:
        return f"ERROR: terminal dependency is missing: {exc.name}"

    action = text_or_default(action).strip().lower()
    session = text_or_default(session, "main").strip() or "main"
    command = text_or_default(command)
    keys = text_or_default(keys)
    wait = min(int_or_default(wait_ms, 700, minimum=0), 10_000)
    cols_supplied = cols is not None
    rows_supplied = rows is not None
    width = min(int_or_default(cols, DEFAULT_COLS, minimum=1), 400)
    height = min(int_or_default(rows, DEFAULT_ROWS, minimum=1), 200)
    sessions = context.terminal_sessions

    if action != "start" and (cols_supplied or rows_supplied):
        return "ERROR: cols and rows apply only to action=start"

    if action == "list":
        return _report(
            {
                "sessions": [
                    {
                        "session": name,
                        "command": state["command"],
                        "cwd": str(state["cwd"]),
                        "alive": state["child"].isalive(),
                    }
                    for name, state in sorted(sessions.items())
                ]
            },
            context,
        )

    if action == "start":
        if not command.strip():
            return "ERROR: action=start requires command"
        workdir = context.resolve_path(cwd) if cwd else context.cwd.resolve()
        if not workdir.is_dir():
            return f"ERROR: no such directory: {workdir}"
        shell = os.environ.get("SHELL") or "/bin/sh"
        try:
            child = pexpect.spawn(
                shell,
                ["-c", command],
                cwd=str(workdir),
                dimensions=(height, width),
                timeout=None,
                encoding=None,
                env={
                    **os.environ,
                    "TERM": "xterm-256color",
                    "COLUMNS": str(width),
                    "LINES": str(height),
                },
            )
        except (OSError, pexpect.ExceptionPexpect) as exc:
            return f"ERROR: could not start terminal command: {type(exc).__name__}: {exc}"
        screen = pyte.Screen(width, height)
        replacement = {
            "child": child,
            "screen": screen,
            "stream": pyte.Stream(screen),
            "command": command,
            "cwd": workdir,
            "snapshot_n": 0,
            "previous_lines": None,
        }
        _LIVE_CHILDREN.add(child)
        old = sessions.get(session)
        if old is not None:
            _stop_state(old)
        sessions[session] = replacement
        return _observe(sessions[session], session, f"started {command}", wait, context)

    state = sessions.get(session)
    if state is None:
        return f"ERROR: no terminal session named {session!r}; use action=start first"

    if action == "stop":
        sessions.pop(session, None)
        _stop_state(state)
        return _report({"session": session, "stopped": True}, context)

    if action == "look":
        return _observe(state, session, "looked", wait, context)

    if action == "send":
        if not state["child"].isalive():
            return f"ERROR: terminal session {session!r} has exited"
        sent: list[str] = []
        for raw_token in keys.split(","):
            token = raw_token.strip()
            if not token:
                continue
            value = "," if token.lower() == "comma" else _KEY_SEQUENCES.get(
                token.lower(), token
            )
            state["child"].send(value.encode("utf-8"))
            sent.append(token)
            time.sleep(0.06)
        return _observe(state, session, f"sent {sent}", wait, context)

    return f"ERROR: action must be one of start, send, look, stop, list; got {action!r}"


def _draw_screen(screen: Any, path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    size = 16
    font = None
    for candidate in _FONT_CANDIDATES:
        if not Path(candidate).exists():
            continue
        try:
            font = ImageFont.truetype(candidate, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    box = font.getbbox("M")
    cell_width = max(1, box[2] - box[0])
    cell_height = int(size * 1.35)
    image = Image.new(
        "RGB",
        (cell_width * screen.columns + 8, cell_height * screen.lines + 8),
        _BACKGROUND,
    )
    draw = ImageDraw.Draw(image)
    for y in range(screen.lines):
        row = screen.buffer[y]
        for x in range(screen.columns):
            cell = row[x]
            char = cell.data or " "
            foreground = _colour(getattr(cell, "fg", "default"), _FOREGROUND)
            background = _colour(getattr(cell, "bg", "default"), _BACKGROUND)
            if getattr(cell, "reverse", False):
                foreground, background = background, foreground
            px, py = 4 + x * cell_width, 4 + y * cell_height
            if background != _BACKGROUND:
                draw.rectangle(
                    [px, py, px + cell_width, py + cell_height], fill=background
                )
            if char != " ":
                draw.text((px, py), char, font=font, fill=foreground)
    image.save(path, format="PNG")


def terminal_snapshot(
    session: str | None = "main",
    output_path: str | None = "",
    wait_ms: int | None = 100,
    context: ToolContext | None = None,
) -> str:
    """Render one live PTY screen to PNG and return it through js vision handling."""
    if context is None:
        return "ERROR: missing ToolContext"
    session = text_or_default(session, "main").strip() or "main"
    state = context.terminal_sessions.get(session)
    if state is None:
        return f"ERROR: no terminal session named {session!r}; use terminal_session first"
    raw_output_path = text_or_default(output_path)
    if raw_output_path and Path(raw_output_path).suffix.lower() != ".png":
        return "ERROR: output_path must end in .png"
    _drain(state, min(int_or_default(wait_ms, 100, minimum=0), 10_000))
    state["previous_lines"] = _render_lines(state["screen"])
    snapshot_n = state["snapshot_n"] + 1
    if raw_output_path:
        target = context.resolve_path(raw_output_path)
    else:
        safe_session = re.sub(r"[^a-zA-Z0-9_.-]+", "-", session).strip("-") or "main"
        target = (
            state["cwd"]
            / "terminal-snapshots"
            / f"{safe_session}-{snapshot_n:02d}.png"
        )
    try:
        context.snapshot(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        _draw_screen(state["screen"], target)
    except Exception as exc:
        return f"ERROR: could not render terminal snapshot: {type(exc).__name__}: {exc}"
    state["snapshot_n"] = snapshot_n
    return fs.fs_read(file_path=str(target), context=context)


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "terminal_session",
            load_description("terminal_session"),
            terminal_session,
            {
                "action": {"type": "string", "enum": ["start", "send", "look", "stop", "list"]},
                "session": {"type": "string", "default": "main"},
                "command": {"type": "string"},
                "keys": {"type": "string"},
                "cwd": {"type": "string"},
                "wait_ms": {"type": "integer", "minimum": 0, "maximum": 10_000, "default": 700},
                "cols": {"type": "integer", "minimum": 1, "maximum": 400},
                "rows": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            required=("action",),
        ),
        Tool(
            "terminal_snapshot",
            load_description("terminal_snapshot"),
            terminal_snapshot,
            {
                "session": {"type": "string", "default": "main"},
                "output_path": {"type": "string"},
                "wait_ms": {"type": "integer", "minimum": 0, "maximum": 10_000, "default": 100},
            },
        ),
    )
