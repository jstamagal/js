"""Tool exchanges at ui.tools 0-3, control-byte cleaning, and transcript markers."""

from __future__ import annotations

import io
import re
from types import SimpleNamespace

from js import display, runtime
from js.toolkit import Tool, ToolContext, ToolRegistry
from js.transcript import TranscriptLogSink

SGR = re.compile(r"\x1b\[[0-9;]*m")

RESULT_292 = "\n".join(f"{index}:ab|row {index} of the file" for index in range(1, 293))


def _plain(text: str) -> list[str]:
    return SGR.sub("", text).splitlines()


def _metrics(line: str) -> tuple[list[int], list[int]]:
    """(byte numbers, line numbers) from a metrics line."""
    match = re.search(r"(\d+(?:/\d+)?)B (\d+(?:/\d+)?)L$", line)
    assert match, line
    return [int(n) for n in match.group(1).split("/")], [int(n) for n in match.group(2).split("/")]


def test_clean_strips_osc_csi_and_control_bytes_but_keeps_newline_and_tab():
    raw = "a\x1b]0;title\x07b\x1b[31mc\x1b[0m\td\x00e\x07f\x1bcg\x0eh\r\ni\x9b2Jj\x1b(0k\x1bPq#0\x1b\\l"

    assert display.clean(raw) == "abc\tdefgh\nijkl"


def test_292_line_result_at_each_tools_level():
    total_bytes = len(RESULT_292.encode())

    assert display.render_tool_result("read", RESULT_292, 0) == ""

    level1 = _plain(display.render_tool_result("read", RESULT_292, 1))
    assert len(level1) == 1
    assert _metrics(level1[0]) == ([total_bytes], [292])

    level2 = _plain(display.render_tool_result("read", RESULT_292, 2, preview=24, width=200))
    assert level2[:24] == RESULT_292.split("\n")[:24]
    assert level2[24] == "..."
    assert len(level2) == 26
    shown_bytes = len("\n".join(RESULT_292.split("\n")[:24]).encode())
    assert _metrics(level2[25]) == ([shown_bytes, total_bytes], [24, 292])

    level3 = _plain(display.render_tool_result("read", RESULT_292, 3))
    assert level3[:292] == RESULT_292.split("\n")
    assert len(level3) == 293
    assert _metrics(level3[292]) == ([total_bytes], [292])


def test_level_two_result_that_fits_has_no_ellipsis():
    out = _plain(display.render_tool_result("read", "one\ntwo", 2, preview=12))

    assert out[:2] == ["one", "two"]
    assert "..." not in out
    assert _metrics(out[-1]) == ([7], [2])


def test_result_with_escape_sequences_and_nul_bytes_leaves_the_terminal_sane():
    hostile = "ok\x1b]0;pwned\x07\x1b[2J\x1bc\x00\x0e\x9b1mdone\n\x1b(0lqk\x1b[?1049h"

    for level in (1, 2, 3):
        out = display.render_tool_result("shell", hostile, level)
        # Only the renderer's own colour sequences remain.
        stripped = SGR.sub("", out)
        assert "\x1b" not in stripped
        assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", stripped)
    assert "okdone" in _plain(display.render_tool_result("shell", hostile, 3))


def test_escape_sequences_in_tool_arguments_do_not_reach_the_terminal():
    args = {"command": "echo \x1b]0;x\x07hi\x1b[2J", "cwd": "/tmp\x1bc"}

    for level in (2, 3):
        stripped = SGR.sub("", display.render_tool_call("shell", args, level))
        assert "\x1b" not in stripped


def test_shell_envelope_is_dropped_and_exit_shown_only_when_nonzero():
    ok = "shell=/bin/zsh\nexit=0\n--- stdout ---\nhello\nworld"
    failed = "shell=/bin/zsh\nexit=2\n--- stdout ---\nhello\n--- stderr ---\nboom"

    ok_lines = _plain(display.render_tool_result("shell", ok, 3))
    assert ok_lines[:2] == ["hello", "world"]
    assert not any("shell=" in line or "---" in line or "exit" in line for line in ok_lines)
    assert _metrics(ok_lines[-1]) == ([11], [2])

    failed_line = _plain(display.render_tool_result("shell", failed, 1))[0]
    assert re.search(r"\bexit 2\b", failed_line)
    assert _metrics(failed_line)[1] == [2]


def test_command_is_capped_by_lines_at_level_two_and_whole_at_level_three():
    script = "python3 - <<'EOF'\n" + "\n".join(f"print({n})" for n in range(30)) + "\nEOF"
    args = {"command": script}

    level2 = _plain(display.render_tool_call("shell", args, 2, preview=5))
    assert sum("print(" in line for line in level2) == 4
    assert any("+27" in line for line in level2)

    level3 = _plain(display.render_tool_call("shell", args, 3, preview=5))
    assert sum("print(" in line for line in level3) == 30
    assert display.render_tool_call("shell", args, 1) == ""


def test_one_marker_per_exchange_at_every_visible_level():
    args = {"file_path": "x.py"}
    for level in (1, 2, 3):
        text = display.render_tool_call("read", args, level) + display.render_tool_result("read", RESULT_292, level)
        markers = [line for line in _plain(text) if line.startswith(display.TOOL_MARKER + " ")]
        assert len(markers) == 1, level
        assert markers[0].isascii()


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
        context.config = SimpleNamespace(settings={"ui": {"tools": level}})
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
    context.config = SimpleNamespace(settings={"ui": {"tools": 0}})

    runtime._dispatch_tool_calls(
        [runtime._PendingToolCall("c1", "lines", ["{}"])], runtime.Telemetry(None), 256 * 1024, True,
        runtime.ToolErrorTracker(), _registry(), context,
    )

    assert capsys.readouterr().out == ""


def test_level_three_read_highlights_the_file_text_and_keeps_every_byte():
    result = '1:ab|def f(x):\n2:cd|    return "s"\n3:ef|\n[9 total lines; continue with {}]'

    rendered = display.render_tool_result("read", result, 3, args={"file_path": "x.py"}, width=80)
    rows = rendered.splitlines()

    assert _plain(rendered)[:4] == result.split("\n")
    assert SGR.sub("", rows[0]).startswith("1:ab|def")
    # The source is highlighted; the paging note is not.
    assert SGR.search(rows[0].removeprefix("1:ab|"))
    assert rows[3] == "[9 total lines; continue with {}]" + display.C.RESET


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
    context.config = SimpleNamespace(settings={"ui": {"tools": 2}})
    calls = [
        runtime._PendingToolCall("c1", "task", ['{"prompt": "slow"}']),
        runtime._PendingToolCall("c2", "task", ['{"prompt": "fast"}']),
    ]

    runtime._dispatch_tool_calls(
        calls, runtime.Telemetry(None), 256 * 1024, True, runtime.ToolErrorTracker(), registry, context,
    )

    lines = _plain(capsys.readouterr().out)
    headers = [i for i, line in enumerate(lines) if line.startswith(display.TOOL_MARKER + " ")]
    assert len(headers) == 2
    for start, end in zip(headers, headers[1:] + [len(lines)], strict=True):
        prompt = "slow" if "slow" in lines[start] else "fast"
        assert f"result-for-{prompt}" in lines[start + 1:end]
