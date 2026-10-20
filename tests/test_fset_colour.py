"""%Cnn in a piece's format reaches the screen as colour, and NO_COLOR=1
takes every colour away."""

from __future__ import annotations

from js import display, fset, screen

RED = "38;2;255;0;0"


def _reasoning() -> str:
    scroll = screen.Scrollback()
    block = scroll.reasoning(2)
    block.append("thinking\n")
    return scroll.buffer.text


def _exchange() -> str:
    shell = "shell=bash\nexit=0\n--- stdout ---\nok\n"
    return display.render_exchange("shell", {"command": "make"}, shell, level=1, width=60)


def test_palette_codes_in_piece_formats_are_drawn_in_colour(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    fset.set_entry("REASONING_HEADING", "%C04HEAD%C")
    fset.set_entry("TOOL_RESULT_LEAD", "%C04>>%C")
    assert f"{RED}mHEAD" in _reasoning()
    assert f"{RED}m>>" in _exchange()


def test_no_color_takes_the_palette_codes_away(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    fset.set_entry("REASONING_HEADING", "%C04HEAD%C")
    fset.set_entry("TOOL_RESULT_LEAD", "%C04>>%C")
    assert "HEAD" in _reasoning() and RED not in _reasoning()
    assert ">>" in _exchange() and RED not in _exchange()
