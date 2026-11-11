"""Settings values retain their content and column when they span rows."""

import json

import pytest

from js import cli, display, fset, messages, setcmd, settings


@pytest.mark.parametrize("columns", [20, 40, 50, 60, 100])
@pytest.mark.parametrize("key", ["LSP_SERVERS", "MCP_SERVERS"])
def test_settings_values_wrap_under_the_value(monkeypatch, capsys, columns, key):
    monkeypatch.setattr(display, "terminal_width", lambda: columns)
    monkeypatch.setattr(cli, "_live_settings_overlay_for_state", lambda _cfg, _state: {})
    values = {}
    spec = settings.spec_for(key)
    settings.set_dotted(values, spec.path, {"example": {"command": "word " * 60, "url": "https://example.org/" + "a" * 180}})
    cli._show_settings({"settings": values}, None, key)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) > 1
    assert all(display.cells(line) < columns for line in lines)
    value = setcmd.render_value(spec, settings.knob(values, key), masked=setcmd.masks_secrets(values))
    original = messages.SETTING_IS.plain(key=key, value=value)
    marker = messages.SETTING_IS.plain(key=key, value="\x00")
    indent = display.cells(marker.partition("\x00")[0])
    if indent > (columns - 1) // 2:
        indent = min(4, (columns - 1) // 2)
    assert all(line.startswith(" " * indent) for line in lines[1:])
    assert "".join("".join(lines).split()) == "".join(original.split())


def test_settings_wrap_retains_custom_format_colour_and_live_source(monkeypatch):
    monkeypatch.setattr(messages, "is_terminal", lambda _stream: True)
    monkeypatch.setattr(fset, "colour_on", lambda: True)
    monkeypatch.setattr(messages.SETTING_IS.__class__, "format", lambda self: "%C04$0 => %C03$1- END" if self is messages.SETTING_IS else self.default_format())
    values = {"banner": "visible banner"}
    result = setcmd.show_lines_effective(values, {"BANNER": setcmd.LiveValue("wide 界 " * 30, "test-source")}, "BANNER", columns=50)
    lines = result.lines
    assert all(display.cells(line) <= 50 for line in lines)
    assert any("\x1b[" in line for line in lines)
    plain = " ".join(fset.plain(line).strip() for line in lines)
    assert "END" in plain and "live: test-source" in plain
    assert plain.count("界") == 30


@pytest.mark.parametrize("fmt", ["$0", "$1- and $1-"])
def test_settings_custom_format_without_unique_value_stays_bounded(monkeypatch, fmt):
    monkeypatch.setattr(messages.SETTING_IS.__class__, "format", lambda self: fmt if self is messages.SETTING_IS else self.default_format())
    result = setcmd.show_lines_effective({}, {"BANNER": setcmd.LiveValue("x" * 150, "source")}, "BANNER", columns=30)
    assert all(display.cells(line) <= 30 for line in result.lines)
    assert "".join(result.lines).count("x") == (300 if fmt.count("$1-") == 2 else 0)


def test_settings_list_wraps_and_still_masks_secret_values():
    values = {"PARANOID_MODE": "on"}
    spec = settings.spec_for("MCP_SERVERS")
    settings.set_dotted(values, spec.path, {"secret": "private" * 30})
    result = setcmd.show_lines_effective(values, key="*_SERVERS", columns=40)
    assert all(display.cells(line) <= 40 for line in result.lines)
    plain = "".join(result.lines)
    assert "MCP_SERVERS" in plain
    assert "private" not in plain


def test_hanging_rows_keeps_multiline_colour_and_words():
    parts = [("#ff0000", "label: one two three\nfour five six")]
    rows = display.hanging_rows(parts, 15, 7)
    assert all(display.cells(row) <= 15 for row in rows)
    assert all(fset.plain(row).startswith(" " * 7) for row in rows[1:])
    assert " ".join(fset.plain(row).strip() for row in rows) == "label: one two three four five six"


@pytest.mark.parametrize("width,indent", [(10, 9), (2, 1)])
def test_hanging_rows_keeps_room_for_wide_characters(width, indent):
    rows = display.hanging_rows([("", "labelxxx 界界界")], width, indent)
    assert all(display.cells(row) <= width for row in rows)
    assert "".join(rows).count("界") == 3


@pytest.mark.parametrize("columns", [40, 50, 100])
def test_set_confirmation_wraps_and_preserves_ordinary_words(monkeypatch, capsys, columns):
    monkeypatch.setattr(display, "terminal_width", lambda: columns)
    raw = json.dumps({"example": {"command": "pyright-langserver", "url": "https://example.org/" + "x" * 180}})
    assert cli._cmd_set("LSP_SERVERS " + raw, {"settings": {}}, None) is None
    lines = capsys.readouterr().out.splitlines()
    assert all(display.cells(line) < columns for line in lines)
    assert any("pyright-langserver" in line for line in lines)
    original = messages.SETTING_CHANGED.plain(key="LSP_SERVERS", value=raw)
    assert "".join("".join(lines).split()) == "".join(original.split())


def test_unset_confirmation_wraps_restored_value():
    spec = settings.spec_for("LSP_SERVERS")
    values, baseline = {}, {}
    settings.set_dotted(values, spec.path, {"old": "short"})
    settings.set_dotted(baseline, spec.path, {"new": "word " * 50})
    result = setcmd.set_command(values, "-LSP_SERVERS", baseline, columns=50)
    assert not result.error
    assert all(display.cells(line) <= 50 for line in result.lines)
    assert "".join(result.lines).count("word") == 50


def test_live_custom_prefix_is_included_in_value_alignment(monkeypatch):
    original_format = messages.Message.format
    monkeypatch.setattr(messages.Message, "format", lambda self: "prefix $0-" if self is messages.SETTING_LIVE else original_format(self))
    value = "word " * 40
    result = setcmd.show_lines_effective({}, {"BANNER": setcmd.LiveValue(value, "source")}, "BANNER", columns=100)
    marker = messages.SETTING_LIVE.plain(line=messages.SETTING_IS.said(key="BANNER", value="\x00"), source="source")
    indent = display.cells(marker.partition("\x00")[0])
    assert all(fset.plain(line).startswith(" " * indent) for line in result.lines[1:])
    assert all(display.cells(line) <= 100 for line in result.lines)
