"""Tool exchanges at TOOLS_DISPLAY 0-3, control-byte cleaning, and transcript markers."""

from __future__ import annotations

import io
import re
from types import SimpleNamespace

from js import display, runtime
from js import messages as msgs
from js.toolkit import Tool, ToolContext, ToolRegistry
from js.transcript import TranscriptLogSink

SGR = re.compile(r"\x1b\[[0-9;]*m")

RESULT_292 = "\n".join(f"{index}|row {index} of the file" for index in range(1, 293))


def _plain(text: str) -> list[str]:
    """The rows of `text` without colour or the exchange rail; blank rows dropped."""
    rows = [row.removeprefix(display.RAIL).removeprefix(" ") for row in SGR.sub("", text).splitlines()]
    return [row for row in rows if row.strip()]


def _shown(name: str, result, level: int, *, args=None, **options) -> str:
    """A finished exchange."""
    return display.render_exchange(name, args, result, level=level, **options)


def _running(name: str, args, level: int, **options) -> str:
    """An exchange whose call has not returned."""
    return display.render_exchange(name, args, None, level=level, **options)


MARKS = (display.MARK, display.OK, display.FAIL)


def _headers(lines: list[str]) -> list[str]:
    """Each exchange's header with the call's last word, on the row under
    it, joined on."""
    headers: list[str] = []
    for line in lines:
        if line.startswith(MARKS):
            headers.append(line)
        elif headers and line.lstrip().startswith(msgs.TOOL_RESULT_LEAD.text().strip()):
            headers[-1] += " " + line.strip()
    return headers


def test_clean_strips_osc_csi_and_control_bytes_but_keeps_newline_and_tab():
    raw = "a\x1b]0;title\x07b\x1b[31mc\x1b[0m\td\x00e\x07f\x1bcg\x0eh\r\ni\x9b2Jj\x1b(0k\x1bPq#0\x1b\\l"

    assert display.clean(raw) == "abc\tdefgh\nijkl"


def test_292_line_result_at_each_tools_level():
    rows = [f"row {index} of the file" for index in range(1, 293)]

    assert _shown("read", RESULT_292, 0) == ""
    assert len(_plain(_shown("read", RESULT_292, 1))) == 1

    level2 = "\n".join(_plain(_shown("read", RESULT_292, 2, preview=24, width=200)))
    assert [row for row in rows if row in level2] == rows[:24]

    level3 = "\n".join(_plain(_shown("read", RESULT_292, 3, width=200)))
    assert all(row in level3 for row in rows)


def test_result_with_escape_sequences_and_nul_bytes_leaves_the_terminal_sane():
    hostile = "ok\x1b]0;pwned\x07\x1b[2J\x1bc\x00\x0e\x9b1mdone\n\x1b(0lqk\x1b[?1049h"

    for level in (1, 2, 3):
        out = _shown("shell", hostile, level)
        # Only the renderer's own colour sequences remain.
        stripped = SGR.sub("", out)
        assert "\x1b" not in stripped
        assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", stripped)
    assert "okdone" in SGR.sub("", _shown("shell", hostile, 3))


def test_escape_sequences_in_tool_arguments_do_not_reach_the_terminal():
    args = {"command": "echo \x1b]0;x\x07hi\x1b[2J", "cwd": "/tmp\x1bc"}

    for level in (2, 3):
        stripped = SGR.sub("", _running("shell", args, level))
        assert "\x1b" not in stripped


def test_shell_envelope_is_dropped_and_exit_shown_only_when_nonzero():
    ok = "shell=/bin/zsh\nexit=0\n--- stdout ---\nhello\nworld"
    failed = "shell=/bin/zsh\nexit=2\n--- stdout ---\nhello\n--- stderr ---\nboom"

    shown = SGR.sub("", _shown("shell", ok, 3))
    assert "hello" in shown and "world" in shown
    assert "shell=" not in shown and "---" not in shown and "exit" not in shown

    for level in (1, 2, 3):
        assert re.search(r"\bexit 2\b", SGR.sub("", _shown("shell", failed, level)))


def test_command_is_capped_by_lines_at_level_two_and_whole_at_level_three():
    script = "python3 - <<'EOF'\n" + "\n".join(f"print({n})" for n in range(30)) + "\nEOF"
    args = {"command": script}

    level2 = _plain(_running("shell", args, 2, preview=5))
    assert sum("print(" in line for line in level2) == 4

    level3 = _plain(_running("shell", args, 3, preview=5))
    assert sum("print(" in line for line in level3) == 30
    assert not any("print(" in line for line in _plain(_running("shell", args, 1)))


def test_one_transcript_marker_per_exchange_at_every_visible_level():
    args = {"file_path": "x.py"}
    for level in (1, 2, 3):
        text = _shown("read", RESULT_292, level, args=args)
        markers = [line for line in _plain(display.for_transcript(text))
                   if line.startswith(display.TOOL_MARKER + " ")]
        assert len(markers) == 1, level


def _registry() -> ToolRegistry:
    def lines(**_kwargs):
        return "a\nb\nc"

    def fail(**_kwargs):
        return "ERROR: no such thing"

    tools = (Tool("lines", "lines", lines, {}), Tool("fail", "fail", fail, {}))
    return ToolRegistry(tools=tools, aliases={"lines": "lines", "fail": "fail"})


def test_ui_tools_zero_prints_nothing_for_an_exchange(capsys):
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 0})

    runtime._dispatch_tool_calls(
        [runtime._PendingToolCall("c1", "lines", ["{}"])], runtime.Telemetry(None), 256 * 1024, True,
        runtime.ToolErrorTracker(), _registry(), context,
    )

    assert capsys.readouterr().out == ""


def test_level_three_read_highlights_the_file_text_and_keeps_it_all():
    result = '1|def f(x):\n2|    return "s"\n3|\n[9 total lines; continue with {}]'

    rendered = _shown("read", result, 3, args={"file_path": "x.py"}, width=80)

    shown = SGR.sub("", rendered)
    assert "def f(x):" in shown and 'return "s"' in shown
    code_row = next(row for row in rendered.splitlines() if "def" in SGR.sub("", row))
    assert SGR.search(code_row[code_row.index("def"):])


def test_a_failed_call_shows_the_error_without_the_retry_note_for_the_model():
    result = runtime.ToolErrorTracker().record("write", "ERROR: You must read the file first.\n")

    for level in (1, 2, 3):
        shown = _shown("write", result, level, width=80)
        assert "<retry>" not in shown
        assert "attempts_left" not in shown
    assert "You must read the file first." in _shown("write", result, 3, width=80)


def _traced(calls, *, level: int = 1) -> tuple[list[str], str]:
    """Trace `(name, args, result)` calls one after another on plain stdout;
    returns the header rows shown and the transcript."""
    import contextlib

    log = io.StringIO()
    telemetry = runtime.Telemetry(None, transcript_log=TranscriptLogSink([log], []))
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": level})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        for name, args, result in calls:
            opened = runtime._trace_call(telemetry, context, name, args)
            runtime._trace_result(telemetry, context, name, result, args=args, exchange=opened)
        runtime.settle_tool_trace(telemetry)
    return _headers(_plain(out.getvalue())), log.getvalue()


def _traced_on_screen(calls) -> list[str]:
    """Trace `(name, args, result)` calls one after another on the screen's
    scrollback; returns the header rows it shows."""
    from js import screen

    scroll = screen.Scrollback()
    scroll.toggle_key = ""

    class Exchange:
        def open(self, rendered):
            self.span = scroll.exchange_open(rendered)

        def update(self, rendered, expanded=None):
            scroll.exchange_update(self.span, rendered, expanded)

        def close(self, rendered, expanded=None):
            scroll.exchange_close(self.span, rendered, expanded)

    telemetry = runtime.Telemetry(None)
    telemetry.exchange_factory = Exchange
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 1})
    for name, args, result in calls:
        opened = runtime._trace_call(telemetry, context, name, args)
        runtime._trace_result(telemetry, context, name, result, args=args, exchange=opened)
    runtime.settle_tool_trace(telemetry)
    scroll.flush()
    return _headers(_plain(scroll.buffer.text))


def test_a_shell_command_is_quiet_only_when_all_of_it_looks_around():
    quiet = ("ls -la", "cd /tmp && git status | head -3", "cat a.txt; wc -l a.txt")
    loud = ("just test", "ls > listing.txt", "git status && git commit -m x", "ls\nrm -rf build")

    assert [display.call_kind("shell", {"command": command}) for command in quiet] == ["quiet"] * len(quiet)
    assert [display.call_kind("shell", {"command": command}) for command in loud] == ["command"] * len(loud)


def test_the_output_of_a_command_still_running_can_be_read():
    import threading
    import time

    from js.toolkit import process_net

    context = ToolContext(cwd="/tmp")
    command = "echo first-line; sleep 30"
    worker = threading.Thread(target=process_net.shell, kwargs={"command": command, "context": context})
    worker.start()
    try:
        tail = None
        deadline = time.monotonic() + 10
        while tail is None and time.monotonic() < deadline:
            tail = process_net.live_tail(context, command, 6)
            time.sleep(0.05)
        assert tail is not None and tail[0] == ["first-line"]
    finally:
        process_net.interrupt_inflight(context)
        worker.join(10)
    assert process_net.live_tail(context, command, 6) is None


def test_ctrl_o_unfolds_a_folded_tool_row_and_folds_it_back():
    from js import screen

    scroll = screen.Scrollback()
    shell = "shell=bash\nexit=0\n--- stdout ---\n" + "\n".join(f"out {n}" for n in range(8)) + "\n"
    folded = display.render_exchange("shell", {"command": "make"}, shell, level=1, width=60)
    whole = display.render_exchange("shell", {"command": "make"}, shell, level=3, width=60)
    span = scroll.exchange_open(folded)
    scroll.exchange_close(span, folded, expanded=whole)
    scroll.append("after\n")

    def rows() -> list[str]:
        return [row for row in SGR.sub("", scroll.buffer.text).splitlines() if row.strip()]

    shut = rows()
    assert "out 0" not in "".join(shut) and shut[-1] == "after"
    assert len(shut[0]) > len(SGR.sub("", folded).strip().splitlines()[0])  # the hint rides on the row
    assert scroll.toggle_reasoning()
    opened = rows()
    assert all(f"out {n}" in "".join(opened) for n in range(8)) and opened[-1] == "after"
    scroll.toggle_reasoning()
    assert rows() == shut


def test_a_level_one_exchange_hands_the_screen_its_whole_form(monkeypatch):
    closed: list[tuple[str, str | None]] = []

    class Exchange:
        def open(self, rendered):
            pass

        def update(self, rendered, expanded=None):
            pass

        def close(self, rendered, expanded=None):
            closed.append((rendered, expanded))

    telemetry = runtime.Telemetry(None)
    telemetry.exchange_factory = Exchange
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 1})
    result = "shell=bash\nexit=0\n--- stdout ---\none\ntwo\nthree\n"
    opened = runtime._trace_call(telemetry, context, "shell", {"command": "make"})
    runtime._trace_result(telemetry, context, "shell", result, args={"command": "make"}, exchange=opened)
    for name in ("read", "read"):
        opened = runtime._trace_call(telemetry, context, name, {"path": "/w/a.py"})
        runtime._trace_result(telemetry, context, name, "1|alpha_line", args={"path": "/w/a.py"}, exchange=opened)
    runtime.settle_tool_trace(telemetry)
    assert closed and all(expanded for _, expanded in closed)
    assert "one" in SGR.sub("", closed[0][1]) and "alpha_line" in SGR.sub("", closed[-1][1])


def test_a_quiet_call_still_running_when_a_loud_one_starts_keeps_its_whole_form():
    closed: list[tuple[str, str | None]] = []

    class Exchange:
        def open(self, rendered):
            pass

        def update(self, rendered, expanded=None):
            pass

        def close(self, rendered, expanded=None):
            closed.append((SGR.sub("", rendered), expanded and SGR.sub("", expanded)))

    telemetry = runtime.Telemetry(None)
    telemetry.exchange_factory = Exchange
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 1})
    read_args, shell_args = {"path": "/w/a.py"}, {"command": "make"}
    reading = runtime._trace_call(telemetry, context, "read", read_args)
    making = runtime._trace_call(telemetry, context, "shell", shell_args)
    runtime._trace_result(telemetry, context, "read", "1|alpha_line", args=read_args, exchange=reading)
    runtime._trace_result(telemetry, context, "shell", "shell=bash\nexit=0\n--- stdout ---\nok\n",
                          args=shell_args, exchange=making)
    runtime.settle_tool_trace(telemetry)
    reads = [expanded for rendered, expanded in closed if " read " in rendered]
    assert reads and reads[0] and "alpha_line" in reads[0]


def test_a_failed_repeat_of_a_quiet_call_leaves_the_earlier_result_in_the_whole_form():
    closed: list[str | None] = []

    class Exchange:
        def open(self, rendered):
            pass

        def update(self, rendered, expanded=None):
            pass

        def close(self, rendered, expanded=None):
            closed.append(expanded and SGR.sub("", expanded))

    telemetry = runtime.Telemetry(None)
    telemetry.exchange_factory = Exchange
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 1})
    one, two = {"path": "/w/a.py"}, {"path": "/w/a.py"}
    first = runtime._trace_call(telemetry, context, "read", one)
    runtime._trace_result(telemetry, context, "read", "1|alpha_line", args=one, exchange=first)
    again = runtime._trace_call(telemetry, context, "read", two)
    runtime._trace_result(telemetry, context, "read", "ERROR: gone", args=two, exchange=again)
    runtime.settle_tool_trace(telemetry)
    assert any(expanded and "alpha_line" in expanded for expanded in closed), closed


def test_a_resumed_tool_row_folds_with_its_whole_form_like_a_live_one(capsys):
    from js import cli, settings

    closed: list[str | None] = []

    class Exchange:
        def open(self, rendered):
            pass

        def update(self, rendered, expanded=None):
            pass

        def close(self, rendered, expanded=None):
            closed.append(expanded)

    output = "shell=bash\nexit=0\n--- stdout ---\n" + "\n".join(f"line {n}" for n in range(20)) + "\n"
    messages = [
        {"role": "user", "content": "build it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "shell", "arguments": '{"command": "make"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": output},
        {"role": "assistant", "content": "done"},
    ]
    telemetry = runtime.Telemetry(None)
    telemetry.exchange_factory = Exchange
    state = {"messages": messages, "settings": settings.seed_defaults()}
    cli._show_resume(state, telemetry, 60)
    assert closed and closed[0] and all(f"line {n}" in SGR.sub("", closed[0]) for n in range(20))
    shown = SGR.sub("", capsys.readouterr().out)
    assert "build it" in shown and "done" in shown


def test_a_finished_quiet_call_unfolds_before_its_run_ends(monkeypatch):
    from js import screen

    scroll = screen.Scrollback()
    scroll.toggle_key = "c-o"

    class Exchange:
        def open(self, rendered):
            self.span = scroll.exchange_open(rendered)

        def update(self, rendered, expanded=None):
            scroll.exchange_update(self.span, rendered, expanded)

        def close(self, rendered, expanded=None):
            scroll.exchange_close(self.span, rendered, expanded)

    telemetry = runtime.Telemetry(None)
    telemetry.exchange_factory = Exchange
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 1})
    for _ in range(2):
        opened = runtime._trace_call(telemetry, context, "read", {"path": "/w/a.py"})
        runtime._trace_result(telemetry, context, "read", "1|alpha_line", args={"path": "/w/a.py"}, exchange=opened)
    assert scroll.toggle_reasoning()
    assert "alpha_line" in SGR.sub("", scroll.buffer.text)


def test_the_transcript_keeps_every_call_whole_at_every_level():
    shell = "shell=bash\nexit=0\n--- stdout ---\nfirst_out\nsecond_out\nthird_out\n"
    for level in (1, 2):
        _, transcript = _traced([
            ("shell", {"command": "make"}, shell),
            ("read", {"file_path": "/w/alpha.py"}, "1|alpha_line"),
        ], level=level)
        for kept in ("first_out", "second_out", "third_out", "alpha_line"):
            assert kept in transcript, (level, kept)


def test_a_long_run_of_quiet_calls_renders_each_call_a_fixed_number_of_times(monkeypatch):
    real = display.render_exchange
    count = [0]

    def counting(*args, **kwargs):
        count[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(display, "render_exchange", counting)

    def renders(calls: int) -> int:
        count[0] = 0
        _traced_on_screen([("read", {"file_path": f"/w/f{n}.py"}, f"1|line {n}") for n in range(calls)])
        return count[0]

    assert renders(32) <= 4 * renders(8) + 4


def test_a_quiet_call_shown_only_when_it_returns_unfolds_at_once():
    # A call made inside a kernel cell reaches the trace only with its result.
    from js import screen

    scroll = screen.Scrollback()
    scroll.toggle_key = "c-o"

    class Exchange:
        def open(self, rendered):
            self.span = scroll.exchange_open(rendered)

        def update(self, rendered, expanded=None):
            scroll.exchange_update(self.span, rendered, expanded)

        def close(self, rendered, expanded=None):
            scroll.exchange_close(self.span, rendered, expanded)

    telemetry = runtime.Telemetry(None)
    telemetry.exchange_factory = Exchange
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 1})
    runtime._trace_result(telemetry, context, "read", "1|nested_line", args={"path": "/w/a.py"})
    assert scroll.toggle_reasoning()
    assert "nested_line" in SGR.sub("", scroll.buffer.text)


def test_plain_stdout_keeps_what_tools_display_hides_and_shows_a_subagent_start():
    import contextlib

    log = io.StringIO()
    telemetry = runtime.Telemetry(None, transcript_log=TranscriptLogSink([log], []))
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 1, "TOOLS_PREVIEW_LINES": 3})
    big = "\n".join(f"line {n}" for n in range(500))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        opened = runtime._trace_call(telemetry, context, "fetch_all", {"url": "x"})
        runtime._trace_result(telemetry, context, "fetch_all", big, args={"url": "x"}, exchange=opened)
        tasks = {"tasks": ["review 9b142f1"]}
        opened = runtime._trace_call(telemetry, context, "coder", tasks)
        started = _plain(out.getvalue())
        runtime._trace_result(telemetry, context, "coder", "verdict: problem", args=tasks, exchange=opened)
        runtime.settle_tool_trace(telemetry)

    shown = _plain(out.getvalue())
    assert "line 2" in "\n".join(shown) and "line 3" not in "\n".join(shown)
    assert "line 499" in log.getvalue()
    assert any("coder" in row for row in started)
