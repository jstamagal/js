"""Clipboard images in the input line.

The `PASTE_IMAGE_KEY` key reads the image on the clipboard, keeps its bytes
here, and inserts `[image #N]` at the cursor. When the line is submitted,
`attach.split_repl_attachments` turns each kept placeholder into an attachment,
and `attach.build_user_message` sends its bytes the way it sends an `@path`
image. Placeholders are numbered from 1 for the life of the process, so a line
recalled from history in the same process still carries its image.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.filters import emacs_insert_mode, vi_insert_mode
from prompt_toolkit.key_binding import DynamicKeyBindings, KeyBindings, KeyBindingsBase

from . import messages as msgs
from . import output
from . import settings as _settings
from .toolkit.fs import _detect_visual_mime

WAYLAND_COMMAND = ("wl-paste", "--type", "image/png")
X11_COMMAND = ("xclip", "-selection", "clipboard", "-t", "image/png", "-o")
# Over ssh there is no display; kitty forwards its clipboard through the tty.
KITTY_COMMAND = ("kitten", "clipboard", "-g", "-m", "image/png", "/dev/stdout")
# A name without a suffix, so the paste check takes the mime type from the bytes.
_DETECT_NAME = Path("clipboard")

# ASCII digits, no leading zero, bounded: `[image #01]` is not image 1, and a
# long digit run cannot hit int()'s digit limit.
_PLACEHOLDER = re.compile(r"\[image #([1-9][0-9]{0,8})\]")
_images: dict[int, bytes] = {}


class ClipboardError(Exception):
    """The clipboard gave no image. ``message`` and ``fields`` are the one line
    the operator sees."""

    def __init__(self, message: msgs.Message, **fields: object) -> None:
        super().__init__(message.said(**fields))
        self.message = message
        self.fields = fields


def placeholder(n: int) -> str:
    return msgs.IMAGE_PLACEHOLDER.format(n=n)


def keep(data: bytes) -> str:
    """Keep ``data`` and return the placeholder that names it."""
    n = len(_images) + 1
    _images[n] = data
    return placeholder(n)


def lookup(token: str) -> bytes | None:
    """The bytes a placeholder names; None for any other text."""
    match = _PLACEHOLDER.fullmatch(token)
    return _images.get(int(match.group(1))) if match else None


def placeholders(text: str) -> list[str]:
    """The kept placeholders in ``text``, in order, each once."""
    found: list[str] = []
    for match in _PLACEHOLDER.finditer(text):
        token = match.group(0)
        if int(match.group(1)) in _images and token not in found:
            found.append(token)
    return found


def clipboard_command(settings: Mapping | None, env: Mapping[str, str] | None = None) -> list[str] | None:
    """The command that writes the clipboard image to stdout:
    `PASTE_IMAGE_COMMAND` when set, else wl-paste under Wayland, else xclip
    under X11, else kitten inside a kitty terminal (ssh). None when there is
    no clipboard."""
    configured = _settings.knob(settings, "PASTE_IMAGE_COMMAND")
    if configured:
        # Raises ValueError on an unbalanced quote; read_image reports it.
        return shlex.split(str(configured))
    env = os.environ if env is None else env
    backends = {
        "wayland": (bool(env.get("WAYLAND_DISPLAY")), WAYLAND_COMMAND),
        "x11": (bool(env.get("DISPLAY")), X11_COMMAND),
        "kitty": (env.get("TERM") == "xterm-kitty", KITTY_COMMAND),
    }
    for name in _settings.knob(settings, "PASTE_IMAGE_BACKENDS"):
        available, command = backends.get(name, (False, ()))
        if available:
            return list(command)
    return None


def uses_terminal(settings: Mapping | None, env: Mapping[str, str] | None = None) -> bool:
    """True when the clipboard command talks through the tty, so the input
    app must release it first."""
    try:
        return clipboard_command(settings, env) == list(KITTY_COMMAND)
    except ValueError:
        return False


def read_image(
    settings: Mapping | None,
    *,
    env: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bytes:
    """The image bytes on the clipboard. Raises ClipboardError when there is no
    clipboard, no tool to read it, no image on it, or an image over
    `FILE_BYTES_MAX`."""
    try:
        argv = clipboard_command(settings, env)
    except ValueError as exc:
        raise ClipboardError(msgs.CLIPBOARD_READ_FAILED, tool="PASTE_IMAGE_COMMAND", error=exc) from exc
    if argv is None:
        raise ClipboardError(msgs.CLIPBOARD_NONE)
    if not argv:
        raise ClipboardError(msgs.CLIPBOARD_READ_FAILED, tool="PASTE_IMAGE_COMMAND", error="empty command")
    if shutil.which(argv[0]) is None:
        raise ClipboardError(msgs.CLIPBOARD_TOOL_MISSING, tool=argv[0])
    try:
        done = run(argv, capture_output=True, timeout=float(_settings.knob(settings, "PASTE_IMAGE_TIMEOUT")), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClipboardError(msgs.CLIPBOARD_READ_FAILED, tool=argv[0], error=exc) from exc
    data = done.stdout or b""
    mime = _detect_visual_mime(_DETECT_NAME, data[:16])
    if done.returncode != 0 or not mime or not mime.startswith("image/"):
        raise ClipboardError(msgs.CLIPBOARD_NO_IMAGE)
    limit = int(_settings.knob(settings, "FILE_BYTES_MAX"))
    if len(data) > limit:
        raise ClipboardError(msgs.CLIPBOARD_IMAGE_TOO_LARGE, size=len(data), limit=limit)
    return data


def key_bindings(get_settings: Callable[[], Mapping | None]) -> KeyBindingsBase:
    """Bindings for the paste key, read from `PASTE_IMAGE_KEY` on every key
    press so a `/set` takes effect at once. Unset binds nothing."""
    cache: dict[object, KeyBindings | None] = {}

    def current() -> KeyBindings | None:
        key = _settings.knob(get_settings(), "PASTE_IMAGE_KEY")
        if key not in cache:
            cache.clear()
            cache[key] = _bindings_for(key, get_settings) if _settings.is_key_name(key) else None
        return cache[key]

    return DynamicKeyBindings(current)


def _bindings_for(key: str, get_settings: Callable[[], Mapping | None]) -> KeyBindings:
    kb = KeyBindings()

    @kb.add(*key.split(), filter=emacs_insert_mode | vi_insert_mode)
    async def _paste(event) -> None:
        buffer = event.current_buffer
        settings = get_settings()
        try:
            if uses_terminal(settings):
                data = await run_in_terminal(lambda: read_image(settings), in_executor=True)
            else:
                data = await asyncio.get_running_loop().run_in_executor(None, read_image, settings)
        except ClipboardError as exc:
            await _say(event.app, exc)
            return
        buffer.insert_text(keep(data))

    return kb


async def _say(app, error: ClipboardError) -> None:
    """One line to the operator. The full-screen app routes stdout into its
    scrollback; a prompt session prints above its input line."""
    def show() -> None:
        output.notice(error.message, stderr=False, flush=False, **error.fields)

    if app.full_screen:
        show()
    else:
        await run_in_terminal(show)
