"""Command help fits the terminal without breaking ordinary description words."""

import pytest

from js import cli, display


@pytest.mark.parametrize("columns", [32, 60, 100])
def test_help_wraps_descriptions_under_their_column(monkeypatch, capsys, columns):
    monkeypatch.setattr(display, "terminal_width", lambda: columns)
    monkeypatch.setattr(cli.prompt_commands, "discover", lambda _cwd: {})
    description = "ordinary description words remain readable while browsing command help"
    cli._cmd_help("", {"aliases": {"example": description}}, None)
    lines = capsys.readouterr().out.splitlines()
    assert all(display.cells(line) < columns for line in lines)
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
def test_help_bounds_long_wide_alias_names(monkeypatch, capsys, columns):
    monkeypatch.setattr(display, "terminal_width", lambda: columns)
    monkeypatch.setattr(cli.prompt_commands, "discover", lambda _cwd: {})
    alias = "界" * 100
    cli._cmd_help("", {"aliases": {alias: "short description"}}, None)
    output = capsys.readouterr().out
    assert all(display.cells(line) < columns for line in output.splitlines())
    assert output.count("界") == 100
