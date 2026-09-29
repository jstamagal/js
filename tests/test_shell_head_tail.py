"""Shell output past its budget keeps the head and the tail, and the whole raw
stream is on disk, so every byte stays reachable with `read`."""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

from js import paths
from js.capped_process import _StreamCapture
from js.toolkit import ToolContext
from js.toolkit import fs
from js.toolkit.process_net import shell

MARKER_RE = re.compile(
    r"\[truncated: (?P<knob>[\w.]+) \((?P<limit>\d+)\) reached; (?P<stream>stdout|stderr) "
    r"bytes (?P<first>\d+)-(?P<stop>\d+) of (?P<total>\d+) \(\d+ bytes\) are not shown; "
    r"the whole (?P=stream) is at (?P<path>\S+) — read it on with range (?P<range>\{[^}]*\})\]"
)

# 1 MB of stdout: a first line, a middle token at a known offset, ANSI color
# codes in the middle, and a last line.
MB_COMMAND = (
    "printf 'FIRST_LINE\\n'; head -c 500000 /dev/zero | tr '\\0' a; "
    "printf 'MIDDLE_TOKEN\\033[31mred\\033[0m'; head -c 500000 /dev/zero | tr '\\0' b; "
    "printf '\\nLAST_LINE\\n'"
)
MB_OUTPUT = (
    b"FIRST_LINE\n" + b"a" * 500000 + b"MIDDLE_TOKEN\x1b[31mred\x1b[0m" + b"b" * 500000 + b"\nLAST_LINE\n"
)


def _marker(result: str) -> re.Match[str]:
    match = MARKER_RE.search(result)
    assert match is not None, result[-2000:]
    return match


def test_a_megabyte_of_output_returns_head_and_tail_with_a_marker_naming_the_dropped_middle(tmp_path):
    context = ToolContext(cwd=tmp_path)
    result = shell(MB_COMMAND, timeout=60, context=context)

    assert "exit=0" in result
    assert "FIRST_LINE" in result
    assert "LAST_LINE" in result
    assert "MIDDLE_TOKEN" not in result
    marker = _marker(result)
    first, stop, total = int(marker["first"]), int(marker["stop"]), int(marker["total"])
    assert total == len(MB_OUTPUT)
    assert 0 < first < stop < total
    assert json.loads(marker["range"]) == {"start_byte": first}
    # What is shown is exactly the bytes around the named gap.
    stdout = result.split("--- stdout ---\n", 1)[1]
    assert stdout.startswith(MB_OUTPUT[:first].decode())
    assert stdout.endswith(MB_OUTPUT[stop:].decode())


def test_the_result_fits_the_inline_limit_so_the_runtime_does_not_spill_it_again(tmp_path):
    context = ToolContext(cwd=tmp_path)
    result = shell(MB_COMMAND, timeout=60, context=context)
    assert len(result.encode()) <= context.max_tool_result_inline_bytes
    assert _marker(result)["knob"] == "limits.max_tool_result_inline_bytes"


def test_the_spill_file_holds_the_full_raw_output(tmp_path):
    context = ToolContext(cwd=tmp_path)
    result = shell(MB_COMMAND, timeout=60, context=context)
    spill = Path(_marker(result)["path"])
    assert spill.read_bytes() == MB_OUTPUT
    # The result itself has the escape codes stripped; the file keeps them.
    assert "\x1b[" not in result


def test_read_with_start_byte_reaches_any_byte_of_the_spill_file(tmp_path):
    context = ToolContext(cwd=tmp_path)
    result = shell(MB_COMMAND, timeout=60, context=context)
    spill = _marker(result)["path"]
    middle = MB_OUTPUT.index(b"MIDDLE_TOKEN")
    for offset, expected in ((middle, "MIDDLE_TOKEN"), (len(MB_OUTPUT) - len(b"LAST_LINE\n"), "LAST_LINE"),
                             (0, "FIRST_LINE")):
        read = fs.read(file_path=spill, range={"start_byte": offset}, context=context)
        assert expected in read


def test_output_under_the_budget_is_whole_and_writes_no_file(tmp_path):
    context = ToolContext(cwd=tmp_path)
    spill_dir = paths.tool_results_dir()
    before = set(spill_dir.glob("shell-*"))
    result = shell("printf 'short\\n'", timeout=30, context=context)
    assert "short" in result
    assert "truncated" not in result
    assert set(spill_dir.glob("shell-*")) == before


def test_a_small_stream_is_kept_whole_while_the_other_is_cut(tmp_path):
    context = ToolContext(cwd=tmp_path)
    result = shell("printf 'ERR_LINE\\n' >&2; " + MB_COMMAND, timeout=60, context=context)
    stderr = result.split("--- stderr ---\n", 1)[1]
    assert stderr == "ERR_LINE\n"
    assert _marker(result)["stream"] == "stdout"


def test_output_between_the_budget_and_the_cap_still_names_a_file_with_all_of_it(tmp_path):
    # 20 kB fits the in-memory cap but not an 8 kB inline limit: the file is
    # written when the result is cut.
    context = ToolContext(cwd=tmp_path, max_tool_result_inline_bytes=8192)
    result = shell("head -c 20000 /dev/zero | tr '\\0' z; printf END", timeout=30, context=context)
    marker = _marker(result)
    assert Path(marker["path"]).read_bytes() == b"z" * 20000 + b"END"
    assert result.rstrip().endswith("END")


def test_a_background_job_reports_head_and_tail_of_new_output_and_spills_it_whole(tmp_path):
    context = ToolContext(cwd=tmp_path)
    gate = tmp_path / "gate"
    started = shell(
        "printf 'EARLY\\n'; while [ ! -e gate ]; do sleep 0.01; done; " + MB_COMMAND,
        timeout=1, context=context,
    )
    assert "RUNNING" in started
    handle = started.split("handle ", 1)[1].split(",", 1)[0]
    gate.touch()
    finished = shell(action="wait", handle=handle, timeout=60, context=context)

    assert "EARLY" not in finished  # delivered already
    assert "FIRST_LINE" in finished and "LAST_LINE" in finished
    marker = _marker(finished)
    early = len(b"EARLY\n")
    assert int(marker["total"]) == early + len(MB_OUTPUT)
    assert int(marker["first"]) > early
    assert Path(marker["path"]).read_bytes() == b"EARLY\n" + MB_OUTPUT


def test_a_poll_of_a_running_job_cuts_new_output_the_same_way(tmp_path):
    context = ToolContext(cwd=tmp_path)
    gate = tmp_path / "gate"
    started = shell(
        MB_COMMAND + "; while [ ! -e gate ]; do sleep 0.01; done", timeout=1, context=context,
    )
    handle = started.split("handle ", 1)[1].split(",", 1)[0]
    seen = started
    deadline = time.monotonic() + 30
    while "LAST_LINE" not in seen:
        assert time.monotonic() < deadline
        seen += shell(action="poll", handle=handle, context=context)
    assert "RUNNING" in seen
    marker = _marker(seen)
    assert Path(marker["path"]).read_bytes().startswith(MB_OUTPUT[: int(marker["stop"])])
    gate.touch()
    shell(action="kill", handle=handle, context=context)


def test_capture_reads_any_range_back_exactly(tmp_path):
    rng = random.Random(7)
    data = bytes(rng.randrange(256) for _ in range(50000))
    capture = _StreamCapture(4096, tmp_path / "s.log")
    at = 0
    while at < len(data):
        step = rng.randrange(1, 3000)
        capture.feed(data[at:at + step])
        at += step
    capture.close()
    assert (tmp_path / "s.log").read_bytes() == data
    for _ in range(200):
        a = rng.randrange(len(data))
        b = rng.randrange(a, len(data) + 1)
        assert capture.read(a, b) == data[a:b]


def test_excerpt_splits_on_character_boundaries(tmp_path):
    text = "é" * 5000  # two bytes each
    capture = _StreamCapture(1000, tmp_path / "u.log")
    capture.feed(text.encode())
    excerpt = capture.excerpt(0, 101)
    excerpt.head.decode()
    excerpt.tail.decode()
    first, stop = excerpt.omitted
    assert excerpt.head == text.encode()[:first]
    assert excerpt.tail == text.encode()[stop:]
