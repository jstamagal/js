"""Read coverage must authorize only content actually displayed."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from js.toolkit import fs
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("restart", [False, True])
def test_undo_restores_original_coverage(tmp_path, restart):
    target = tmp_path / "file.txt"
    original = "HEAD\n" + "".join(f"L{i:02d}\n" for i in range(2, 21))
    target.write_text(original)
    context = ToolContext(cwd=tmp_path)
    context.configure_snapshot_store("audit", tmp_path / "session.jsonl", state_dir=tmp_path / "state")
    fs.read(str(target), range={"start_line": 1, "end_line": 1}, context=context)
    assert fs.patch(str(target), old_string="HEAD", new_string="HEAD\n" + "owned\n" * 12, context=context).startswith("patched ")
    if restart:
        context = ToolContext(cwd=tmp_path)
        context.configure_snapshot_store("audit", tmp_path / "session.jsonl", state_dir=tmp_path / "state")
    assert fs.undo(str(target), context=context).startswith("restored ")
    assert context.read_ranges[target] == [(1, 1)]
    assert fs.patch(str(target), old_string="L11", new_string="ELEVEN", context=context).startswith("ERROR:")
    assert target.read_text() == original
    assert fs.patch(str(target), old_string="HEAD", new_string="head", context=context).startswith("patched ")


@pytest.mark.parametrize("separator", list("\v\f\x1c\x1d\x1e\x85\u2028\u2029"))
def test_patch_refuses_unsupported_line_coordinates(tmp_path, separator):
    target = tmp_path / "file.txt"
    original = f"one{separator}two{separator}three\n"
    target.write_text(original)
    context = ToolContext(cwd=tmp_path)
    shown = fs.read(str(target), range={"start_line": 1, "end_line": 1}, context=context)
    assert "three" not in shown
    result = fs.patch(str(target), old_string="three", new_string="THREE", context=context)
    assert result.startswith("ERROR: unsupported line separator")
    assert target.read_text() == original
    assert not context.snapshots


def test_patch_refuses_a_fifo_without_blocking(tmp_path):
    """A FIFO with no writer parks open() forever. The guard must reject it by
    type before reading, so the call runs in a subprocess where a regression
    fails on the timeout instead of hanging the whole suite."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo is unavailable on this platform")
    fifo = tmp_path / "p.fifo"
    os.mkfifo(fifo)
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from js.toolkit import fs\n"
        "from js.toolkit.core import ToolContext\n"
        "target = Path(sys.argv[1])\n"
        "context = ToolContext(cwd=target.parent)\n"
        "result = fs.patch(str(target), old_string='a', new_string='b', context=context)\n"
        "assert result.startswith('ERROR:'), result\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(fifo)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_patch_refuses_a_device_target(tmp_path):
    device = Path("/dev/null")
    if not device.exists():
        pytest.skip("/dev/null is unavailable on this platform")
    context = ToolContext(cwd=tmp_path)

    result = fs.patch(str(device), old_string="a", new_string="b", context=context)

    assert result == f"ERROR: not a regular file: {device}"
