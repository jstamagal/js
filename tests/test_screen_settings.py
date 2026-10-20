from js import fset, screen


def test_registered_scrollback_limit_retains_only_recent_lines():
    fset.bind_settings({"SCROLLBACK_LINES_MAX": 2})
    scroll = screen.Scrollback()
    scroll.append("one\ntwo\nthree")
    assert scroll.buffer.text == "two\nthree"
