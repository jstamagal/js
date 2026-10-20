"""Tool exchanges at TOOLS_DISPLAY 0-3, control-byte cleaning, and transcript markers."""

from __future__ import annotations

import io
import json
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


def test_level_one_line_carries_the_key_argument_and_fits_the_width():
    result = "one\ntwo"
    cases = [
        ("read", {"file_path": "js/display.py", "range": {"start_line": 3}}, "js/display.py"),
        ("fs_search", {"pattern": "def key_argument", "path": "js"}, "def key_argument"),
        ("shell", {"command": "git status --short\ngit log -1"}, "git status --short"),
        ("fetch", {"url": "https://example.com/page"}, "https://example.com/page"),
    ]
    for name, args, shown in cases:
        lines = _plain(_shown(name, result, 1, args=args, width=80))
        assert len(_headers(lines)) == 1 and shown in lines[0], lines
    assert "git log" not in _plain(_shown("shell", result, 1, args=cases[2][1]))[0]

    deep = "/srv/" + "/".join(f"directory{n}" for n in range(20)) + "/target_file.py"
    long_command = "rg --no-heading " + "x" * 200
    for name, args, kept in (("read", {"file_path": deep}, "target_file.py"),
                             ("shell", {"command": long_command}, "rg --no-heading")):
        lines = _plain(_shown(name, result, 1, args=args, width=60))
        assert all(len(line) <= 60 for line in lines), lines
        assert kept in "".join(line.strip() for line in lines)


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


def test_saved_transcript_has_the_ascii_marker_for_every_tool_exchange(capsys):
    for level in (1, 2, 3):
        log = io.StringIO()
        sink = TranscriptLogSink([log], [])
        telemetry = runtime.Telemetry(None, transcript_log=sink)
        context = ToolContext(cwd="/tmp")
        context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": level})
        calls = [
            runtime._PendingToolCall("c1", "lines", ["{}"]),
            runtime._PendingToolCall("c2", "fail", ["{}"]),
            runtime._PendingToolCall("c3", "lines", ["{}"]),
        ]

        runtime._dispatch_tool_calls(
            calls, telemetry, 256 * 1024, True, runtime.ToolErrorTracker(), _registry(), context,
        )

        found = re.findall(rf"^\[\d\d:\d\d\] {re.escape(display.TOOL_MARKER)} (\w+)", log.getvalue(), re.M)
        assert found == ["lines", "fail", "lines"], level
    capsys.readouterr()


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


def test_concurrent_task_exchanges_print_whole_at_level_two(capsys):
    import threading

    first_done = threading.Event()

    def task(prompt: str = "", **_kwargs):
        # "slow" finishes after "fast" so results complete out of call order.
        if prompt == "slow":
            first_done.wait(5)
        else:
            first_done.set()
        return f"result-for-{prompt}"

    registry = ToolRegistry(tools=(Tool("task", "task", task, {}),), aliases={"task": "task"})
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 2})
    calls = [
        runtime._PendingToolCall("c1", "task", ['{"prompt": "slow"}']),
        runtime._PendingToolCall("c2", "task", ['{"prompt": "fast"}']),
    ]

    runtime._dispatch_tool_calls(
        calls, runtime.Telemetry(None), 256 * 1024, True, runtime.ToolErrorTracker(), registry, context,
    )

    lines = _plain(capsys.readouterr().out)
    headers = [i for i, line in enumerate(lines) if line.startswith(MARKS)]
    assert len(headers) == 2
    for start, end in zip(headers, headers[1:] + [len(lines)], strict=True):
        prompt = "slow" if "slow" in lines[start] else "fast"
        assert f"result-for-{prompt}" in "\n".join(lines[start + 1:end])


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


_QUIET_RUN = [
    ("read", {"file_path": "/w/alpha.py"}, "1|alpha_text"),
    ("read", {"file_path": "/w/beta.py"}, "1|beta_text"),
    ("read", {"file_path": "/w/gamma.py"}, "1|gamma_text"),
    ("patch", {"file_path": "/w/c.py", "old_string": "old", "new_string": "new"},
     "patched /w/c.py (1 replacement, hash abc)\n--- a\n+++ b\n@@ -1,1 +1,1 @@\n-old\n+new"),
    ("read", {"file_path": "/w/delta.py"}, "1|delta_text"),
]


def test_a_run_of_quiet_calls_is_one_row_on_the_screen_and_a_call_that_is_not_quiet_ends_it():
    headers = _traced_on_screen(_QUIET_RUN)

    assert len(headers) == 3
    assert all(name in headers[0] for name in ("alpha.py", "beta.py", "gamma.py"))
    assert "patch" in headers[1]
    assert "delta.py" in headers[2] and "alpha.py" not in headers[2]


def test_plain_stdout_shows_every_call_whole_and_the_transcript_records_each():
    # Plain stdout has no key to unfold a folded row, so nothing folds there.
    headers, transcript = _traced(_QUIET_RUN)

    assert len(headers) == 5
    log = _plain(transcript)
    assert all(any(text in row for row in log) for text in ("alpha_text", "beta_text", "gamma_text", "delta_text"))
    assert len(re.findall(rf"{re.escape(display.TOOL_MARKER)} read\b", transcript)) == 4


def test_a_failed_quiet_call_is_shown_with_its_error_outside_the_fold():
    headers = _traced_on_screen([
        ("read", {"file_path": "/w/alpha.py"}, "1|a"),
        ("read", {"file_path": "/w/missing.py"}, "ERROR: no such file: /w/missing.py"),
        ("read", {"file_path": "/w/beta.py"}, "1|b"),
    ])

    failed = [row for row in headers if row.startswith(display.FAIL)]
    assert len(failed) == 1 and "no such file" in failed[0]
    folded = [row for row in headers if row.startswith(display.OK)]
    assert len(folded) == 1 and "missing.py" not in folded[0]


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


def test_no_exchange_row_fills_the_terminal_width(monkeypatch):
    """The scrollback adds a cursor cell after every line; a row as wide as the
    window wraps onto a blank second row."""
    monkeypatch.setattr(display, "terminal_width", lambda: 50)
    shell = "shell=bash\nexit=0\n--- stdout ---\none\ntwo\n"
    for text in (_shown("shell", shell, 2, args={"command": "ls"}),
                 _running("shell", {"command": "ls"}, 1, live=["a", "b"]),
                 display.render_fold("read", [{"path": "/x/a.py"}])):
        assert max(len(row) for row in SGR.sub("", text).splitlines()) < 50


def test_a_long_output_or_diff_line_wraps_and_keeps_its_tail():
    long = "x" * 120 + " TAIL_OF_LINE"
    diff = f"patched /w/c.py (1 replacement, hash abc)\n--- a\n+++ b\n@@ -1,1 +1,1 @@\n-old\n+{long}"
    shell = f"shell=bash\nexit=0\n--- stdout ---\n{long}\nsecond\n"
    for level in (1, 2):
        for text in (_shown("patch", diff, level, args={"file_path": "/w/c.py"}, width=60),
                     _shown("web_thing", f"{long}\nsecond", level, args={"q": "a"}, width=60)):
            assert "TAIL_OF_LINE" in "".join(row.strip() for row in _plain(text)), level
    shown = _shown("shell", shell, 2, args={"command": "x\ny"}, width=60)
    assert "TAIL_OF_LINE" in "".join(row.strip() for row in _plain(shown))


def test_the_run_line_wraps_between_fields_all_through_its_format(monkeypatch, capsys):
    from js import fset

    monkeypatch.setattr(display, "terminal_width", lambda: 41)
    fields = ["model=claude-opus-5-5", "provider=cpa", "base=http://vader:8999/v1", "ctx=1000000",
              "max_out=128000", "effort=high", "vision=on", "tools=13"]
    runtime._print_run_line(fields)
    rows = SGR.sub("", capsys.readouterr().out).splitlines()
    assert len(rows) > 1 and all(len(row) <= 40 for row in rows), rows
    assert [field for row in rows for field in row.split()][2:] == fields
    assert all(row.startswith(" ") for row in rows[1:])

    fset.set_entry("RUN_LINE", "quiet")
    runtime._print_run_line(fields)
    assert SGR.sub("", capsys.readouterr().out) == "quiet\n"


def test_consecutive_exchanges_stack_with_no_blank_row():
    shell = "shell=bash\nexit=0\n--- stdout ---\none\n"
    text = (_shown("shell", shell, 1, args={"command": "make"})
            + _running("shell", {"command": "make"}, 1)
            + display.render_fold("read", [{"path": "/x/a.py"}]))
    assert [row for row in SGR.sub("", text).split("\n")[:-1] if not row.strip()] == []


def test_a_home_path_keeps_its_tilde_when_js_runs_from_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert display.short_path(str(tmp_path / ".js" / "tmp" / "x.txt")) == "~/.js/tmp/x.txt"


def test_a_folded_command_row_keeps_its_payload_and_its_output_apart():
    command = 'bd update js-why.3 --actor="Claude Opus 5.5" --notes "the part that matters PAYLOAD_END"'
    output = "✓ Updated issue: js-why.3 — Panels double-space: blank bar after every line OUTPUT_END"
    shown = _shown("shell", f"shell=bash\nexit=0\n--- stdout ---\n{output}\n", 1,
                   args={"command": command}, width=60)
    rows = [row.strip() for row in _plain(shown)]
    joined = " ".join(rows)
    assert "PAYLOAD_END" in joined.replace(" ", "") and "OUTPUT_END" in joined
    assert "…" not in joined
    assert not any("PAYLOAD_END" in row and "OUTPUT" in row for row in rows)


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


def test_a_huge_folded_row_wraps_in_time_that_grows_with_its_length():
    import time

    def cost(size: int) -> float:
        result = "shell=bash\nexit=0\n--- stdout ---\n" + "x" * size + "\n"
        best = float("inf")
        for _ in range(2):
            start = time.perf_counter()
            display.render_exchange("shell", {"command": "make"}, result, level=1, width=80)
            best = min(best, time.perf_counter() - start)
        return best

    small, large = cost(8_000), cost(32_000)
    assert large < small * 8, (small, large)  # four times the text: linear is ~4x, quadratic ~16x


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


def test_the_whole_form_of_a_message_tool_shows_all_of_its_payload():
    payload = "\n".join(f"payload line {n}" for n in range(20))
    for name, key in (("send", "text"), ("post_task", "body"), ("finish_task", "result")):
        args = {key: payload, "to": "bob", "title": "t"}
        folded = "\n".join(_plain(_shown(name, "Message sent.", 1, args=args, width=60)))
        whole = "\n".join(_plain(_shown(name, "Message sent.", 3, args=args, width=60)))
        assert "payload line 0" in folded
        assert all(f"payload line {n}" in whole for n in range(20)), (name, whole)


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


def test_the_whole_form_keeps_every_argument_whole():
    cwd = "/srv/" + "/".join(f"dir{n}" for n in range(15)) + "/END_OF_CWD"
    note = "first line\nsecond line END_OF_NOTE"
    args = {"command": "make", "cwd": cwd, "note": note}
    whole = "".join(row.strip() for row in _plain(_shown("shell", "shell=bash\nexit=0\n--- stdout ---\nok\n",
                                                          3, args=args, width=60)))
    assert "END_OF_CWD" in whole and "END_OF_NOTE" in whole


def test_a_fold_left_empty_by_its_one_failed_call_draws_no_hint():
    from js import screen

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
    args = {"path": "/w/missing.py"}
    opened = runtime._trace_call(telemetry, context, "read", args)
    runtime._trace_result(telemetry, context, "read", "ERROR: no such file", args=args, exchange=opened)
    runtime.settle_tool_trace(telemetry)
    assert not any(not rendered.strip() and expanded is not None for rendered, expanded in closed), closed

    scroll = screen.Scrollback()
    span = scroll.exchange_open("")
    scroll.exchange_close(span, "", "")
    scroll.append("after\n")
    assert SGR.sub("", scroll.buffer.text) == "after\n"


def test_a_level_two_preview_also_hands_the_screen_its_whole_form():
    closed: list[str | None] = []

    class Exchange:
        def open(self, rendered):
            pass

        def update(self, rendered, expanded=None):
            pass

        def close(self, rendered, expanded=None):
            closed.append(expanded)

    telemetry = runtime.Telemetry(None)
    telemetry.exchange_factory = Exchange
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 2})
    result = "shell=bash\nexit=0\n--- stdout ---\n" + "\n".join(f"row {n}" for n in range(40)) + "\n"
    opened = runtime._trace_call(telemetry, context, "shell", {"command": "make"})
    runtime._trace_result(telemetry, context, "shell", result, args={"command": "make"}, exchange=opened)
    assert closed and closed[0] and "row 39" in SGR.sub("", closed[0])


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


def test_unfolding_a_long_row_and_folding_it_back_loses_no_scrollback(monkeypatch):
    from js import screen

    monkeypatch.setattr(screen, "SCROLLBACK_LINES", 10)
    scroll = screen.Scrollback()
    scroll.toggle_key = "c-o"
    for n in range(5):
        scroll.append(f"early {n}\n")
    whole = "".join(f"result line {n}\n" for n in range(30))
    span = scroll.exchange_open("folded row\n")
    scroll.exchange_close(span, "folded row\n", whole)
    scroll.flush()

    assert scroll.toggle_reasoning()
    unfolded = scroll.buffer.text
    assert "early 0" in unfolded and all(f"result line {n}\n" in unfolded for n in range(30))
    assert scroll.toggle_reasoning()
    assert "early 0" in scroll.buffer.text and "folded row" in scroll.buffer.text


def test_the_model_s_result_cap_leaves_what_the_operator_sees_whole(capsys):
    long = "\n".join(f"WHOLE_LINE_{n}" for n in range(30))

    def lines(**_kwargs):
        return long

    registry = ToolRegistry(tools=(Tool("lines", "lines", lines, {}),), aliases={"lines": "lines"})
    log = io.StringIO()
    telemetry = runtime.Telemetry(None, transcript_log=TranscriptLogSink([log], []))
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 3})
    records = runtime._dispatch_tool_calls(
        [runtime._PendingToolCall("c1", "lines", ["{}"])], telemetry, 64, True, runtime.ToolErrorTracker(),
        registry, context,
    )
    runtime.settle_tool_trace(telemetry)

    sent = str(records[0][-1])
    assert "TOOL_RESULT_MAX" in sent and "WHOLE_LINE_29" not in sent
    shown = capsys.readouterr().out
    assert all(f"WHOLE_LINE_{n}" in shown for n in range(30))
    assert all(f"WHOLE_LINE_{n}" in log.getvalue() for n in range(30))


def test_the_model_s_result_cap_leaves_an_async_tool_s_output_whole(capsys):
    long = "\n".join(f"ASYNC_LINE_{n}" for n in range(30))

    async def lines(**_kwargs):
        return long

    registry = ToolRegistry(tools=(Tool("lines", "lines", lines, {}),), aliases={"lines": "lines"})
    log = io.StringIO()
    telemetry = runtime.Telemetry(None, transcript_log=TranscriptLogSink([log], []))
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 3})
    import asyncio

    async def dispatch():
        return await runtime._dispatch_batch(
            [runtime._PendingToolCall("c1", "lines", ["{}"])], telemetry, 64, True, runtime.ToolErrorTracker(),
            registry, context, asyncio.get_running_loop(),
        )

    records = asyncio.run(dispatch())
    runtime.settle_tool_trace(telemetry)

    assert "TOOL_RESULT_MAX" in str(records[0][-1])
    shown = capsys.readouterr().out
    assert all(f"ASYNC_LINE_{n}" in shown for n in range(30))
    assert all(f"ASYNC_LINE_{n}" in log.getvalue() for n in range(30))


def test_a_tool_that_caps_its_own_result_for_the_model_still_shows_it_whole(capsys):
    from js.toolkit import kernel

    long = "\n".join(f"CELL_LINE_{n}" for n in range(30))
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 3})
    context.max_tool_result_bytes = 64

    def cell(**_kwargs):
        return kernel.cap_for_model(long, context)

    registry = ToolRegistry(tools=(Tool("cell", "cell", cell, {}),), aliases={"cell": "cell"})
    log = io.StringIO()
    telemetry = runtime.Telemetry(None, transcript_log=TranscriptLogSink([log], []))
    records = runtime._dispatch_tool_calls(
        [runtime._PendingToolCall("c1", "cell", ["{}"])], telemetry, 256 * 1024, True, runtime.ToolErrorTracker(),
        registry, context,
    )
    runtime.settle_tool_trace(telemetry)

    assert "CELL_LINE_29" not in str(records[0][-1])
    shown = capsys.readouterr().out
    assert all(f"CELL_LINE_{n}" in shown for n in range(30))
    assert all(f"CELL_LINE_{n}" in log.getvalue() for n in range(30))


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


def _traced_whole(registry, name, context, *, cap=256 * 1024):
    log = io.StringIO()
    telemetry = runtime.Telemetry(None, transcript_log=TranscriptLogSink([log], []))
    records = runtime._dispatch_tool_calls(
        [runtime._PendingToolCall("c1", name, ["{}"])], telemetry, cap, True, runtime.ToolErrorTracker(),
        registry, context,
    )
    runtime.settle_tool_trace(telemetry)
    return str(records[0][-1]), log.getvalue()


def test_a_cut_failure_keeps_its_whole_text_through_the_retry_note(capsys):
    from js.toolkit.core import ToolOutput

    def failing(**_kwargs):
        return ToolOutput("ERROR: it broke [cut]", "ERROR: it broke\n" + "detail\n" * 5 + "UNCUT_TAIL")

    registry = ToolRegistry(tools=(Tool("failing", "failing", failing, {}),), aliases={"failing": "failing"})
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 3})
    sent, log = _traced_whole(registry, "failing", context)
    assert "UNCUT_TAIL" not in sent and "UNCUT_TAIL" in log
    assert "UNCUT_TAIL" in capsys.readouterr().out


def test_a_cut_result_keeps_its_whole_text_through_the_jail_s_path_names(capsys, monkeypatch):
    from js import jail
    from js.toolkit.core import ToolOutput

    monkeypatch.setattr(jail, "active", lambda: object())
    monkeypatch.setattr(jail, "shown", lambda text: text.replace("/host/tmp", "/tmp"))

    def cut(**_kwargs):
        return ToolOutput("/host/tmp/x [cut]", "/host/tmp/x\nUNCUT_TAIL")

    registry = ToolRegistry(tools=(Tool("cut", "cut", cut, {}),), aliases={"cut": "cut"})
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 3})
    sent, log = _traced_whole(registry, "cut", context)
    assert "UNCUT_TAIL" not in sent and "UNCUT_TAIL" in log and "/host/tmp" not in log
    capsys.readouterr()


def test_the_shell_s_whole_output_reaches_the_transcript_past_the_inline_limit(capsys, tmp_path):
    from js.toolkit.registry import build_default_registry

    registry = build_default_registry()
    context = ToolContext(cwd=tmp_path)
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 3})
    context.max_tool_result_inline_bytes = 600
    log = io.StringIO()
    telemetry = runtime.Telemetry(None, transcript_log=TranscriptLogSink([log], []))
    command = json.dumps({"command": "for n in $(seq 1 100); do echo \"numbered row $n\"; done"})
    records = runtime._dispatch_tool_calls(
        [runtime._PendingToolCall("c1", "shell", [command])], telemetry, 256 * 1024, True,
        runtime.ToolErrorTracker(), registry, context,
    )
    runtime.settle_tool_trace(telemetry)
    assert "numbered row 50\n" not in str(records[0][-1])
    shown = capsys.readouterr().out
    assert all(f"numbered row {n}" in log.getvalue() for n in range(1, 101))
    assert all(f"numbered row {n}" in shown for n in range(1, 101))


def test_a_wrapped_diff_row_never_reads_as_another_diff_row():
    diff = "patched /w/c.py (1 replacement, hash abc)\n--- a\n+++ b\n@@ -1,1 +1,1 @@\n-old\n+" + "-" * 150
    for level in (1, 2):
        rows = _plain(_shown("patch", diff, level, args={"file_path": "/w/c.py"}, width=60))
        removed = next(row for row in rows if "- old" in row)
        sign = removed.index("- old")
        assert [row for row in rows if row[sign:sign + 1] == "-"] == [removed], level


def test_a_wrapped_header_starts_no_row_with_its_break_space():
    for width in range(14, 40):
        out = display.render_exchange("shell", {"command": "aaaa bbbb cccc  dddd eeee"}, None, level=1, width=width)
        rows = SGR.sub("", out).rstrip("\n").split("\n")
        for row in rows[1:]:
            text = row.removeprefix(display.RAIL)
            assert text.strip() and not text.startswith(" " * 4), (width, rows)


def test_a_line_with_no_field_starts_no_row_with_its_break_space():
    for width in range(3, 17):
        rows = [display.fset.to_plain(row) for row in display.field_rows([("", "aaaa bbbb  cccc")], width)]
        assert all(row.strip() and not row.startswith(" ") for row in rows), (width, rows)


def test_the_expand_hint_does_not_split_a_wrapped_header(monkeypatch):
    from js import screen

    monkeypatch.setattr(display, "terminal_width", lambda: 80)
    args = {"command": 'bd update js-why.3 --actor="Claude Opus 5.5" --status closed --notes "Panels '
                       'double-space: blank bar after every line (diffs and live shell output) fixed"'}
    result = "shell=/bin/bash\nexit=0\n--- stdout ---\nUpdated issue: js-why.3"
    folded = display.render_exchange("shell", args, result, level=1)
    whole = display.render_exchange("shell", args, result, level=3)
    scroll = screen.Scrollback()
    scroll.toggle_key = "c-o"
    span = scroll.exchange_open(folded)
    scroll.exchange_close(span, folded, whole)
    scroll.flush()
    folded_rows = SGR.sub("", folded).rstrip("\n").split("\n")
    shown = SGR.sub("", scroll.buffer.text).rstrip("\n").split("\n")
    assert len(folded_rows) > 2
    assert shown[:len(folded_rows)] == folded_rows
    assert "c-o" in " ".join(shown[len(folded_rows):])


def test_a_row_shown_whole_already_carries_no_expand_hint():
    closed: list[str | None] = []

    class Exchange:
        def open(self, rendered):
            pass

        def update(self, rendered, expanded=None):
            pass

        def close(self, rendered, expanded=None):
            closed.append(expanded)

    telemetry = runtime.Telemetry(None)
    telemetry.exchange_factory = Exchange
    context = ToolContext(cwd="/tmp")
    context.config = SimpleNamespace(settings={"TOOLS_DISPLAY": 2})
    args = {"command": "ls"}
    opened = runtime._trace_call(telemetry, context, "shell", args)
    runtime._trace_result(telemetry, context, "shell", "shell=/bin/bash\nexit=0\n--- stdout ---\na\nb",
                          args=args, exchange=opened)
    assert closed == [None]
