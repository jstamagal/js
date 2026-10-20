"""Tool exchanges at TOOLS_DISPLAY 0-3, control-byte cleaning, and transcript markers."""

from __future__ import annotations

import io
import re
from types import SimpleNamespace

from js import display, runtime
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
    return [line for line in lines if line.startswith(MARKS)]


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
        assert len(lines) == 1 and shown in lines[0], lines
    assert "git log" not in _plain(_shown("shell", result, 1, args=cases[2][1]))[0]

    deep = "/srv/" + "/".join(f"directory{n}" for n in range(20)) + "/target_file.py"
    long_command = "rg --no-heading " + "x" * 200
    for name, args, kept in (("read", {"file_path": deep}, "target_file.py"),
                             ("shell", {"command": long_command}, "rg --no-heading")):
        line = _plain(_shown(name, result, 1, args=args, width=60))[0]
        assert len(line) <= 60, line
        assert kept in line


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


def test_a_run_of_quiet_calls_is_one_row_and_a_call_that_is_not_quiet_ends_it():
    diff = "patched /w/c.py (1 replacement, hash abc)\n--- a\n+++ b\n@@ -1,1 +1,1 @@\n-old\n+new"
    headers, transcript = _traced([
        ("read", {"file_path": "/w/alpha.py"}, "1|a"),
        ("read", {"file_path": "/w/beta.py"}, "1|b"),
        ("read", {"file_path": "/w/gamma.py"}, "1|c"),
        ("patch", {"file_path": "/w/c.py", "old_string": "old", "new_string": "new"}, diff),
        ("read", {"file_path": "/w/delta.py"}, "1|d"),
    ])

    assert len(headers) == 3
    assert all(name in headers[0] for name in ("alpha.py", "beta.py", "gamma.py"))
    assert "patch" in headers[1]
    assert "delta.py" in headers[2] and "alpha.py" not in headers[2]
    # The transcript records every call on its own.
    assert len(re.findall(rf"{re.escape(display.TOOL_MARKER)} read\b", transcript)) == 4


def test_a_failed_quiet_call_is_shown_with_its_error_outside_the_fold():
    headers, _ = _traced([
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
