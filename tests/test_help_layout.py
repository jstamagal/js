"""Command help fits the terminal without breaking ordinary description words."""

from types import SimpleNamespace

import pytest
from repl_driver import output_of, record_turns, type_into_js

from js import prompt_commands, text_layout


def _help(monkeypatch, tmp_path, capsys, *before: str) -> str:
    """What /help printed in js started in ``tmp_path``, after ``before`` were
    typed."""
    monkeypatch.chdir(tmp_path)
    record_turns(monkeypatch)
    shown: list[str] = []

    async def lines(enter):
        for line in before:
            await enter(line)
        shown.append(await output_of(enter, capsys, "/help"))

    assert type_into_js(monkeypatch, lines) == 0
    return shown[0]


@pytest.mark.parametrize("columns", [32, 60, 100])
def test_help_wraps_descriptions_under_their_column(monkeypatch, tmp_path, capsys, columns):
    monkeypatch.setattr(text_layout, "terminal_width", lambda: columns)
    monkeypatch.setattr(prompt_commands, "discover", lambda _cwd: {})
    description = "ordinary description words remain readable while browsing command help"
    lines = _help(monkeypatch, tmp_path, capsys, f"/alias example {description}").splitlines()
    assert all(text_layout.cells(line) < columns for line in lines)
    start = next(index for index, line in enumerate(lines) if "/example " in line)
    first = lines[start]
    doc_column = first.index("Alias:")
    parts = [first[doc_column:]]
    for line in lines[start + 1:]:
        if line[:doc_column].strip():
            break
        parts.append(line[doc_column:])
    assert " ".join(parts) == f"Alias: {description}"


@pytest.mark.parametrize("columns", [20, 60, 100])
def test_help_bounds_long_wide_alias_names(monkeypatch, tmp_path, capsys, columns):
    monkeypatch.setattr(text_layout, "terminal_width", lambda: columns)
    monkeypatch.setattr(prompt_commands, "discover", lambda _cwd: {})
    alias = "界" * 100
    output = _help(monkeypatch, tmp_path, capsys, f"/alias {alias} short description")
    assert all(text_layout.cells(line) < columns for line in output.splitlines())
    assert output.count("界") == 100


def test_help_keeps_long_prompt_path_and_description_when_wrapped(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(text_layout, "terminal_width", lambda: 60)
    path = "/" + "a" * 200 + "/review.md"
    command = SimpleNamespace(path=path, description="Review a file")
    monkeypatch.setattr(prompt_commands, "discover", lambda _cwd: {"review": command})
    lines = _help(monkeypatch, tmp_path, capsys).splitlines()
    assert all(text_layout.cells(line) < 60 for line in lines)
    start = next(index for index, line in enumerate(lines) if "/review " in line)
    column = lines[start].index("Command file")
    parts = [lines[start][column:]]
    for line in lines[start + 1:]:
        if line[:column].strip():
            break
        parts.append(line[column:])
    assert "".join("".join(parts).split()) == f"Commandfile{path}:Reviewafile"
