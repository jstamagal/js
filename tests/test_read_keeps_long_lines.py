"""A long line must come back whole. The read tool has no column offset, so any
per-line truncation is unrecoverable data loss: the model is told content exists
and given no way to reach it, and it falls back to `shell` to finish the job.
Whole-read caps (max_read_bytes, max_read_lines) and the tool-result spill are
what bound the output."""

from __future__ import annotations

from js.toolkit import ToolContext
from js.toolkit import fs


def test_a_long_line_is_returned_whole(tmp_path):
    long_value = "x" * 64_000
    (tmp_path / "prompt.txt").write_text(f"short\n{long_value}\ntail\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    out = fs.read("prompt.txt", context=context)

    assert long_value in out
    assert "truncated" not in out


def test_every_suffix_keeps_long_lines_whole(tmp_path):
    long_value = "y" * 64_000
    record = f'{{"k":"{long_value}"}}'
    (tmp_path / "data.jsonl").write_text(record + "\n", encoding="utf-8")
    (tmp_path / "data.py").write_text(record + "\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    assert long_value in fs.read("data.jsonl", context=context)
    assert long_value in fs.read("data.py", context=context)


def test_whole_file_byte_cap_still_refuses_with_a_range_hint(tmp_path):
    (tmp_path / "big.txt").write_text("z" * 5_000, encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_read_bytes=1_000)

    out = fs.read("big.txt", context=context)

    assert out.startswith("ERROR:")
    assert 'range={"start_line"' in out
