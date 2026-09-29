"""A spilled tool result is recoverable through the read call its notice names,
including a single-line payload, by byte range."""
from __future__ import annotations

import json
import re

import pytest

from js import runtime, toolkit
from js.toolkit import fs
from js.toolkit.core import ToolContext

_POINTER = re.compile(r"the full text is at (.+) — read it")
_RANGE = re.compile(r"range (\{[^}]*\})")
_CONTINUE = re.compile(r"continue with (\{.*\})\]$")


@pytest.fixture
def context(tmp_path, monkeypatch):
    ctx = ToolContext(cwd=tmp_path, max_tool_result_inline_bytes=4_000)
    monkeypatch.setattr(toolkit, "STOCK_CONTEXT", ctx)
    real_spill = runtime.spill_oversized_result
    monkeypatch.setattr(
        runtime,
        "spill_oversized_result",
        lambda text, cap, **kwargs: real_spill(text, cap, spill_dir=tmp_path / "spill", **kwargs),
    )
    return ctx


def _dispatch_read(context: ToolContext, args: dict) -> str:
    _args, delivered = runtime._dispatch(
        "read", json.dumps(args), runtime.Telemetry(None), cap_bytes=0, tool_context=context
    )
    return delivered


def _spill(context: ToolContext, payload: str) -> tuple[str, str, list[dict]]:
    spilled = runtime._cap_result(payload, 0, context.max_tool_result_inline_bytes)
    pointer = _POINTER.search(spilled)
    assert pointer is not None, spilled
    preview = spilled.rsplit("\n\n[result was ", 1)[0]
    ranges = [json.loads(found) for found in _RANGE.findall(spilled)]
    return pointer.group(1), preview, ranges


def _read_bytes_to_end(context: ToolContext, path: str, first_range: dict) -> str:
    collected: list[str] = []
    args = {"file_path": path, "range": first_range}
    for _ in range(1_000):
        page = _dispatch_read(context, args)
        assert not page.startswith("ERROR"), page
        assert "the full text is at" not in page, "a page of a spill spilled again"
        body, _, footer = page.rpartition("\n")
        collected.append(body)
        more = _CONTINUE.search(footer)
        if more is None:
            return "".join(collected)
        args = json.loads(more.group(1))
    raise AssertionError("byte pages never reached the end of the file")


def test_single_line_payload_is_recovered_whole_by_the_named_byte_range(context):
    instructions = "\n# Driving a pane — é ü 漢字\n\nstep one, step two\n" * 600
    payload = json.dumps({"id": "skill:tuios", "instructions": instructions}, ensure_ascii=False)
    assert "\n" not in payload and len(payload.encode("utf-8")) > 5 * context.max_tool_result_inline_bytes

    path, preview, ranges = _spill(context, payload)

    assert ranges == [{"start_byte": len(preview.encode("utf-8"))}]
    assert preview + _read_bytes_to_end(context, path, ranges[0]) == payload


def test_multiline_payload_names_both_byte_and_line_recovery(context):
    payload = "".join(f"record {n:05d} " + "x" * 60 + "\n" for n in range(400))

    path, preview, ranges = _spill(context, payload)

    by_byte = next(r for r in ranges if "start_byte" in r)
    by_line = next(r for r in ranges if "start_line" in r)
    assert preview + _read_bytes_to_end(context, path, by_byte) == payload
    page = _dispatch_read(context, {"file_path": path, "range": by_line, "show_line_numbers": False})
    resumed_at = payload.splitlines()[by_line["start_line"] - 1]
    assert page.startswith(resumed_at)
    assert preview.splitlines()[-1] in resumed_at


def test_byte_range_reads_whole_utf8_characters(tmp_path):
    target = tmp_path / "accents.txt"
    target.write_text("é" * 100, encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    page = fs.fs_read(file_path=str(target), range={"start_byte": 1, "end_byte": 51}, context=context)

    body = page.rpartition("\n")[0]
    assert body and set(body) == {"é"}


def test_byte_range_reads_past_the_file_size_cap(tmp_path):
    target = tmp_path / "huge.log"
    target.write_bytes(b"a" * 4_000 + b"TAIL-MARKER")
    context = ToolContext(cwd=tmp_path, max_file_bytes=1_000)

    page = fs.fs_read(file_path=str(target), range={"start_byte": 4_000}, context=context)

    assert page.startswith("TAIL-MARKER")


def test_lines_a_byte_range_shows_whole_count_as_read(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("aaa\nbbb\nccc\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    fs.fs_read(file_path=str(target), range={"start_byte": 4, "end_byte": 8}, context=context)
    unseen = fs.patch(file_path=str(target), old_string="ccc", new_string="CCC", context=context)
    seen = fs.patch(file_path=str(target), old_string="bbb", new_string="BBB", context=context)

    assert unseen.startswith("ERROR")
    assert seen.startswith("patched "), seen
    assert target.read_text(encoding="utf-8") == "aaa\nBBB\nccc\n"


def test_line_and_byte_range_together_are_refused(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("aaa\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = fs.fs_read(file_path=str(target), range={"start_line": 1, "start_byte": 0}, context=context)

    assert result.startswith("ERROR")


@pytest.mark.parametrize("bad", [{"start_byte": -5}, {"start_byte": True}, {"end_byte": "x"}, {"start_byte": 0, "end_byte": -1}])
def test_an_invalid_byte_offset_is_refused_not_read_as_the_whole_file(tmp_path, bad):
    target = tmp_path / "f.txt"
    target.write_text("abc\ndef\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = fs.fs_read(file_path=str(target), range=bad, context=context)

    assert result.startswith("ERROR")
    assert target not in context.read_paths
