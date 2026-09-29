"""The vi input buffer's `:` ex line: ex commands act on the buffer, command
words go to the command table, program names edit the buffer."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
import sys

import pytest

from js import cli, exline, paths, settings


class FakeEditor:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.sent: list[str] = []

    def insert(self, text: str) -> None:
        self.text += text

    async def submit(self) -> None:
        self.sent.append(self.text)
        self.text = ""

    async def run(self, argv: list[str]) -> None:
        subprocess.run(argv, check=False)


def _run(line: str, editor: FakeEditor, dispatched: list[str] | None = None, commands=("set", "quit")):
    async def dispatch(command_line: str) -> None:
        (dispatched if dispatched is not None else []).append(command_line)

    asyncio.run(exline.run_ex(line, editor, is_command=lambda verb: verb in commands, dispatch=dispatch))


def _program(tmp_path, name: str, body: str) -> None:
    """An executable on PATH that edits the file it is given."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text(f"#!{sys.executable}\nimport sys, pathlib\np = pathlib.Path(sys.argv[-1])\n{body}\n",
                      encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"


def test_w_writes_the_buffer_and_keeps_it(tmp_path):
    editor = FakeEditor("draft line")
    target = tmp_path / "draft.txt"

    _run(f"w {target}", editor)
    _run("w", editor)

    assert target.read_text(encoding="utf-8").strip() == "draft line"
    assert (paths.notes_dir() / exline.BUFFER_FILE).read_text(encoding="utf-8").strip() == "draft line"
    assert editor.text == "draft line"
    assert editor.sent == []


def test_x_sends_the_buffer():
    editor = FakeEditor("send me")

    _run("x", editor)

    assert editor.sent == ["send me"]


def test_q_quits_through_the_command_table():
    dispatched: list[str] = []

    _run("q", FakeEditor(), dispatched)

    assert dispatched == ["/quit"]


def test_command_words_dispatch_through_the_table():
    dispatched: list[str] = []
    editor = FakeEditor("unsent")

    _run("set model some/model", editor, dispatched)

    assert dispatched == ["/set model some/model"]
    assert editor.text == "unsent"
    assert editor.sent == []


@pytest.mark.parametrize("verb", ["set", "Set"])
def test_ex_set_model_changes_the_model(tmp_path, verb):
    """`:set model X` from the ex line reaches the live model; the table's verbs
    are case-insensitive there as everywhere."""
    state = {"messages": [], "system": "sys", "settings": settings.seed_defaults(), "model": "old/model"}

    async def dispatch(command_line: str) -> None:
        cli._handle_command(command_line, state, None)

    asyncio.run(exline.run_ex(f"{verb} model new/model", FakeEditor(), is_command=lambda v: v in cli.COMMANDS,
                              dispatch=dispatch))

    assert state["model"] == "new/model"


def test_program_name_edits_the_buffer_and_returns_it_unsent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.environ["PATH"])
    _program(tmp_path, "fakevim", "p.write_text(p.read_text() + ' edited\\n')")
    editor = FakeEditor("hello")
    dispatched: list[str] = []

    _run("fakevim", editor, dispatched)

    assert editor.text == "hello edited"
    assert editor.sent == []
    assert dispatched == []


def test_e_opens_editor_on_the_buffer_and_returns_it_unsent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.environ["PATH"])
    _program(tmp_path, "fakeed", "p.write_text('written in the editor\\nsecond line\\n')")
    monkeypatch.setenv("VISUAL", "fakeed")
    editor = FakeEditor("start")

    _run("e", editor)

    assert editor.text == "written in the editor\nsecond line"
    assert editor.sent == []


def test_r_inserts_a_file(tmp_path):
    source = tmp_path / "snippet.txt"
    source.write_text("inserted", encoding="utf-8")
    editor = FakeEditor("before ")

    _run(f"r {source}", editor)

    assert editor.text == "before inserted"


def test_unknown_word_changes_nothing():
    editor = FakeEditor("keep")
    dispatched: list[str] = []

    _run("definitely-not-a-program-xyz", editor, dispatched)

    assert editor.text == "keep"
    assert dispatched == []


def test_n_appends_a_timestamped_note_and_sends_nothing():
    editor = FakeEditor("the message")
    dispatched: list[str] = []

    _run("n remember the milk", editor, dispatched)
    _run("n second", editor, dispatched)

    lines = (paths.notes_dir() / exline.NOTES_FILE).read_text(encoding="utf-8").splitlines()
    assert [re.sub(r"^\[[^\]]+\] ", "", line) for line in lines] == ["remember the milk", "second"]
    assert all(re.match(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ", line) for line in lines)
    assert editor.text == "the message"
    assert editor.sent == []
    assert dispatched == []


def test_bare_n_opens_the_notes_file_in_the_editor(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.environ["PATH"])
    _program(tmp_path, "fakeed", "p.write_text(p.read_text() + 'typed in the notes buffer\\n')")
    monkeypatch.setenv("VISUAL", "fakeed")
    exline.append_note("first")
    editor = FakeEditor("the message")

    _run("n", editor)

    notes = (paths.notes_dir() / exline.NOTES_FILE).read_text(encoding="utf-8")
    assert notes.endswith("typed in the notes buffer\n")
    assert editor.text == "the message"
    assert editor.sent == []
