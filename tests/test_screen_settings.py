from js import fset, screen


def test_registered_scrollback_limit_retains_only_recent_lines():
    fset.bind_settings({"SCROLLBACK_LINES_MAX": 2})
    scroll = screen.Scrollback()
    scroll.append("one\ntwo\nthree")
    assert scroll.buffer.text == "two\nthree"


def test_exit_arm_uses_registered_timeout(monkeypatch):
    from types import SimpleNamespace

    from prompt_toolkit.keys import Keys

    fset.bind_settings({"EXIT_ARM_TIMEOUT": 17.0})
    app, _ = screen.build_app(prompt="", history=None, completer=None,
                              on_line=lambda line: None, on_interrupt=lambda: False,
                              on_eof=lambda: None, application_factory=lambda **kwargs: SimpleNamespace(**kwargs))
    # The input's first interrupt arms the expiry timer.
    buffer = app.layout.current_buffer
    buffer.text = "unfinished"
    scheduled = []
    loop = SimpleNamespace(time=lambda: 0, call_at=lambda at, *args: scheduled.append(at))
    monkeypatch.setattr(screen.asyncio, "get_running_loop", lambda: loop)
    handler = app.key_bindings.get_bindings_for_keys((Keys.ControlC,))[-1].handler
    handler(SimpleNamespace(current_buffer=buffer))
    assert buffer.text == ""
    assert scheduled == [17.0]
