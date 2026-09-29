"""js/messages.py: every operator-facing string is a named entry, banner lines
go through the slot, and severity is colour, never a word."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from js import cli, colors as C, messages as msgs, stream_transport

JS_ROOT = Path(msgs.__file__).parent
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# The commit helper's output is read by the commit agent, not the operator.
MODEL_FACING = {JS_ROOT / "commit_helper.py"}
DEAD = ("error:", "warning:", "js:", "note:", "(no ", "knob", "!!")


def _entries() -> dict[str, msgs.Message]:
    return {name: value for name, value in vars(msgs).items() if isinstance(value, msgs.Message)}


def _plain(text: str) -> str:
    return ANSI.sub("", text)


def test_a_banner_line_starts_with_the_slot_and_a_plain_line_does_not(monkeypatch):
    monkeypatch.setattr(msgs, "BANNER", "@@")
    assert _plain(msgs.Message("Model: {model}").line(model="m")).startswith("@@ ")
    assert not _plain(msgs.Message("{model}", banner=False).line(model="m")).startswith("@@")


def test_command_output_reaches_the_screen_through_the_slot(monkeypatch, capsys):
    monkeypatch.setattr(msgs, "BANNER", "@@")
    state = {"provider_id": None, "provider_base_url": None, "provider_api_key": None}
    cli._cmd_baseurl("https://example.test/v1", state, object())
    cli._handle_command("/alias nope", {"aliases": {}}, object())
    out = [_plain(line) for line in capsys.readouterr().out.splitlines()]
    assert out == [f"@@ {msgs.BASE_URL_SET.text()}", f"@@ {msgs.NO_ALIAS.text(name='nope')}"]
    assert state["provider_base_url"] == "https://example.test/v1"


def test_network_lines_go_through_the_slot(monkeypatch):
    monkeypatch.setattr(msgs, "BANNER", "@@")
    lines: list[str] = []
    stream_transport.install_sink(stream_transport.NetSink(level=lambda: 2, emit=lines.append))
    try:
        stream_transport.begin_call("https://api.example.test/v1", "m")
    finally:
        stream_transport.install_sink(None)
    assert lines and all(_plain(line).startswith("@@ ") for line in lines)


@pytest.mark.parametrize(("severity", "colour"), [(msgs.GRAVE, C.BR_RED), (msgs.WARN, C.BR_YELLOW)])
def test_severity_paints_the_holes_not_the_line(severity, colour):
    line = msgs.Message("Model {model} not saved: {error}", severity).line(model="m1", error="disk full")
    assert f"{colour}m1{C.RESET}" in line
    assert f"{colour}disk full{C.RESET}" in line
    assert f"{colour}Model" not in line


def test_a_message_without_holes_is_painted_whole():
    line = msgs.Message("Prompt is empty.", msgs.GRAVE).line()
    assert line == msgs.banner(f"{C.BR_RED}Prompt is empty.{C.RESET}")


def test_info_is_not_coloured():
    assert "\x1b" not in msgs.Message("Model: {model}").line(model="m")


def test_format_specs_and_conversions_survive_painting():
    entry = msgs.Message("{fullness:.0%} {name!r}", msgs.WARN)
    assert _plain(entry.line(fullness=0.5, name="x")) == msgs.banner(entry.text(fullness=0.5, name="x"))


def test_text_is_the_message_without_slot_or_colour(monkeypatch):
    monkeypatch.setattr(msgs, "BANNER", "@@")
    text = msgs.Message("Model {model} not saved", msgs.GRAVE).text(model="m1")
    assert "@@" not in text and "\x1b" not in text and "m1" in text


@pytest.mark.parametrize("name", sorted(_entries()))
def test_no_entry_carries_the_slot_a_severity_word_or_a_paren(name):
    template = _entries()[name].template
    assert msgs.BANNER not in template
    assert "(" not in template and ")" not in template
    assert not any(word in template.lower() for word in DEAD)


def _string_parts(node: ast.AST) -> list[str]:
    parts: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            parts.append(child.value)
    return parts


def _screen_calls(tree: ast.AST) -> list[ast.Call]:
    """print(...) calls and writes to sys.stdout / sys.stderr."""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "print":
            calls.append(node)
        elif (isinstance(func, ast.Attribute) and func.attr == "write"
              and isinstance(func.value, ast.Attribute) and func.value.attr in ("stdout", "stderr")):
            calls.append(node)
    return calls


def _modules() -> list[Path]:
    return [path for path in sorted(JS_ROOT.rglob("*.py")) if path not in MODEL_FACING]


def test_no_print_in_js_carries_a_dead_word_or_the_slot():
    found = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _screen_calls(tree):
            for text in _string_parts(call):
                lowered = text.lower()
                if msgs.BANNER in text or any(word in lowered for word in DEAD):
                    found.append(f"{path.relative_to(JS_ROOT)}:{call.lineno}: {text!r}")
    assert found == []


def test_the_slot_is_spelled_only_in_messages():
    spelled = [str(path.relative_to(JS_ROOT)) for path in _modules()
               if path.name != "messages.py" and f'"{msgs.BANNER}' in path.read_text(encoding="utf-8")]
    assert spelled == []
