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
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol

from . import messages as msgs
from . import fset, output, pastes, paths

NOTES_FILE = "notes.txt"
BUFFER_FILE = "buffer.txt"


class Editor(Protocol):
    text: str

    def insert(self, text: str) -> None: ...

    def submit(self) -> Awaitable[None]: ...

    def run(self, argv: list[str]) -> Awaitable[int | None]: ...


def editor_argv() -> list[str]:
    return shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")


def append_note(text: str) -> Path:
    """Append one timestamped line to the notes file."""
    path = paths.notes_dir() / NOTES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now().strftime(fset.setting_value("NOTE_TIMESTAMP_FORMAT"))}] {text}\n")
    return path


async def edit_buffer(editor: Editor, argv: list[str], path: Path | None = None) -> None:
    """Run ``argv`` on ``path`` (default: a temp file holding the buffer); the
    buffer takes the file's text afterwards. Nothing is sent."""
    temp = path is None
    if path is None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8", dir=paths.tmp_dir()) as fh:
            fh.write(pastes.expand(editor.text))
        path = Path(fh.name)
    try:
        status = await editor.run([*argv, str(path)])
        # A nonzero exit (vim's :cq) means abort: keep the buffer as it was.
        if status in (None, 0) and path.is_file():
            editor.text = path.read_text(encoding="utf-8").removesuffix("\n")
    finally:
        if temp:
            path.unlink(missing_ok=True)


Dispatch = Callable[[str], Awaitable[None]]
ExCommand = Callable[[str, Editor, Dispatch], Awaitable[None]]


def _write_file(arg: str, editor: Editor) -> Path:
    path = Path(arg).expanduser() if arg else paths.notes_dir() / BUFFER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pastes.expand(editor.text) + "\n", encoding="utf-8")
    return path


async def _write_quit(arg: str, editor: Editor, dispatch: Dispatch) -> None:
    _write_file(arg, editor)
    await editor.submit()


async def _write(arg: str, editor: Editor, dispatch: Dispatch) -> None:
    path = _write_file(arg, editor)
    output.notice(msgs.BUFFER_WRITTEN, stderr=False, flush=False, path=path)


async def _submit(arg: str, editor: Editor, dispatch: Dispatch) -> None:
    await editor.submit()


async def _quit(arg: str, editor: Editor, dispatch: Dispatch) -> None:
    await dispatch(f"/quit {arg}".rstrip())


async def _edit(arg: str, editor: Editor, dispatch: Dispatch) -> None:
    await edit_buffer(editor, editor_argv(), Path(arg).expanduser() if arg else None)


async def _read(arg: str, editor: Editor, dispatch: Dispatch) -> None:
    editor.insert(Path(arg).expanduser().read_text(encoding="utf-8"))


async def _note(arg: str, editor: Editor, dispatch: Dispatch) -> None:
    if arg:
        append_note(arg)
    else:
        notes = paths.notes_dir() / NOTES_FILE
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.touch()
        await editor.run([*editor_argv(), str(notes)])


EX_COMMANDS: dict[str, ExCommand] = {
    "wq": _write_quit, "w": _write, "x": _submit, "q": _quit,
    "e": _edit, "r": _read, "n": _note,
}
EX_FORCE_FORMS = {verb + "!": verb for verb in ("q", "w", "e", "x", "wq")}


async def run_ex(
    line: str,
    editor: Editor,
    *,
    is_command: Callable[[str], bool],
    dispatch: Callable[[str], Awaitable[None]],
    program_allowed: Callable[[str], bool] | None = None,
    commands: Mapping[str, ExCommand] | None = None,
    force_forms: Mapping[str, str] | None = None,
) -> None:
    """Run one ex line. ``dispatch`` takes a `/command` line for the table."""
    verb, _, arg = line.strip().removeprefix("/").partition(" ")
    arg = arg.strip()
    table = EX_COMMANDS if commands is None else commands
    aliases = EX_FORCE_FORMS if force_forms is None else force_forms
    verb = aliases.get(verb, verb)
    try:
        if (command := table.get(verb)) is not None:
            await command(arg, editor, dispatch)
        elif is_command(verb.lower()):
            await dispatch(f"/{verb} {arg}".rstrip())
        elif (shutil.which(verb) if program_allowed is None else program_allowed(verb)):
            await edit_buffer(editor, [verb, *shlex.split(arg)])
        else:
            output.notice(msgs.EX_UNKNOWN, stderr=False, flush=False, verb=verb)
    except (OSError, UnicodeError, ValueError) as e:
        output.notice(msgs.EX_FAILED, stderr=False, flush=False, verb=verb, error=f"{type(e).__name__}: {e}")
