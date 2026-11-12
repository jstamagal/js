"""Writes reject nonregular targets before reading or recording an undo."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from js.toolkit import fs
from js.toolkit.core import ToolContext


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="mkfifo is unavailable")
def test_write_refuses_fifo_without_blocking_or_snapshot(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    script = """
import stat
import sys
from pathlib import Path
from js.toolkit import fs
from js.toolkit.core import ToolContext

target = Path(sys.argv[1])
context = ToolContext(cwd=target.parent)
result = fs.write(str(target), content='replacement', overwrite=True, context=context)
assert result.startswith('ERROR:'), result
assert stat.S_ISFIFO(target.stat().st_mode)
assert not context.snapshots
assert not context.file_hashes
assert not context.read_ranges
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(fifo)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert stat.S_ISFIFO(fifo.stat().st_mode)


@pytest.mark.parametrize("kind", ["directory", "device"])
def test_write_refuses_other_nonregular_files_without_snapshot(tmp_path, kind):
    target = tmp_path if kind == "directory" else Path("/dev/null")
    if not target.exists():
        pytest.skip(f"{target} is unavailable")
    context = ToolContext(cwd=tmp_path)

    result = fs.write(str(target), content="replacement", overwrite=True, context=context)

    assert result.startswith("ERROR:")
    assert not context.snapshots
    assert not context.file_hashes
    assert not context.read_ranges


def test_write_follows_regular_file_symlink(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    context = ToolContext(cwd=tmp_path)
    fs.read(str(link), context=context)

    result = fs.write(str(link), content="after\n", overwrite=True, context=context)

    assert result.startswith("wrote ")
    assert target.read_bytes() == b"after\n"
    assert link.is_symlink()
    assert fs.undo(str(link), context=context).startswith("restored ")
    assert target.read_bytes() == b"before\n"
