"""The `:` ex line of the vi input buffer.

A real ex command does the ex thing to the input buffer: `:w [file]` writes
it and keeps it, `:x` sends it, `:q` quits, `:e [file]` edits it in $EDITOR
and brings the text back unsent, `:r file` inserts a file, `:n [text]` keeps a
note. Any other word that names a command runs through the command table
(`:set model X`). A word that names a program runs that program on the buffer
(`:nvim`), which comes back unsent like `:e`.
"""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from . import colors as C
from . import paths

NOTES_FILE = "notes.txt"
BUFFER_FILE = "buffer.txt"


class Editor(Protocol):
    text: str

    def insert(self, text: str) -> None: ...

    def submit(self) -> Awaitable[None]: ...

    def run(self, argv: list[str]) -> Awaitable[None]: ...


def editor_argv() -> list[str]:
    return shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")


def append_note(text: str) -> Path:
    """Append one timestamped line to the notes file."""
    path = paths.notes_dir() / NOTES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    return path


async def edit_buffer(editor: Editor, argv: list[str], path: Path | None = None) -> None:
    """Run ``argv`` on ``path`` (default: a temp file holding the buffer); the
    buffer takes the file's text afterwards. Nothing is sent."""
    temp = path is None
    if path is None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write(editor.text)
        path = Path(fh.name)
    try:
        await editor.run([*argv, str(path)])
        if path.is_file():
            editor.text = path.read_text(encoding="utf-8").removesuffix("\n")
    finally:
        if temp:
            path.unlink(missing_ok=True)


async def run_ex(
    line: str,
    editor: Editor,
    *,
    is_command: Callable[[str], bool],
    dispatch: Callable[[str], Awaitable[None]],
) -> None:
    """Run one ex line. ``dispatch`` takes a `/command` line for the table."""
    verb, _, arg = line.strip().removeprefix("/").partition(" ")
    arg = arg.strip()
    try:
        if verb == "w":
            path = Path(arg).expanduser() if arg else paths.notes_dir() / BUFFER_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(editor.text + "\n", encoding="utf-8")
            print(f"{C.GREY}(buffer written to {path}; not sent){C.RESET}")
        elif verb == "x":
            await editor.submit()
        elif verb == "q":
            await dispatch(f"/quit {arg}".rstrip())
        elif verb == "e":
            await edit_buffer(editor, editor_argv(), Path(arg).expanduser() if arg else None)
        elif verb == "r":
            editor.insert(Path(arg).expanduser().read_text(encoding="utf-8"))
        elif verb == "n":
            if arg:
                append_note(arg)
            else:
                notes = paths.notes_dir() / NOTES_FILE
                notes.parent.mkdir(parents=True, exist_ok=True)
                notes.touch()
                await editor.run([*editor_argv(), str(notes)])
        elif is_command(verb):
            await dispatch(f"/{verb} {arg}".rstrip())
        elif shutil.which(verb):
            await edit_buffer(editor, [verb, *shlex.split(arg)])
        else:
            print(f"{C.ORANGE}not an editor command, js command or program: {verb}{C.RESET}")
    except (OSError, UnicodeError, ValueError) as e:
        print(f"{C.ORANGE}:{verb}: {type(e).__name__}: {e}{C.RESET}")
