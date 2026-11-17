"""patch skips an identical edit, read takes the range shapes models send and
streams a line range of a file over FILE_BYTES_MAX, and a missing path comes
back with directions to what exists."""
from __future__ import annotations

import json

import pytest
from turn_driver import config, run

from js.toolkit import build_default_registry, file_search, fs, patch
from js.toolkit.core import ToolContext


def _read_file(tmp_path, name: str, body: str) -> tuple:
    target = tmp_path / name
    target.write_text(body, encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    fs.read(name, context=context)
    return target, context


def test_a_batch_with_an_identical_edit_applies_the_others(tmp_path):
    target, context = _read_file(tmp_path, "doc.md", "## Purpose\nalpha\nbeta\n")

    result = patch.patch(
        file_path="doc.md",
        edits=[
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "## Purpose", "new_string": "## Purpose"},
            {"old_string": "beta", "new_string": "BETA"},
        ],
        context=context,
    )

    assert not result.startswith("ERROR"), result
    assert target.read_text(encoding="utf-8") == "## Purpose\nALPHA\nBETA\n"
    assert "edit 2" in result.splitlines()[0]


def test_an_edit_identical_after_line_ending_normalization_is_skipped(tmp_path):
    target, context = _read_file(tmp_path, "crlf.txt", "one\r\ntwo\r\n")

    result = patch.patch(
        file_path="crlf.txt",
        edits=[
            {"old_string": "one\ntwo", "new_string": "one\r\ntwo"},
            {"old_string": "two", "new_string": "TWO"},
        ],
        context=context,
    )

    assert not result.startswith("ERROR"), result
    assert target.read_bytes() == b"one\r\nTWO\r\n"


def test_a_patch_whose_every_edit_is_identical_changes_nothing_and_is_no_error(tmp_path):
    target, context = _read_file(tmp_path, "doc.md", "## Purpose\nbody\n")
    before = target.stat().st_mtime_ns

    result = patch.patch(
        file_path="doc.md",
        edits=[{"old_string": "## Purpose", "new_string": "## Purpose"},
               {"old_string": "body", "new_string": "body"}],
        context=context,
    )

    assert not result.startswith("ERROR")
    assert target.read_text(encoding="utf-8") == "## Purpose\nbody\n"
    assert target.stat().st_mtime_ns == before


@pytest.fixture
def numbered(tmp_path):
    target = tmp_path / "numbers.txt"
    target.write_text("".join(f"row {n}\n" for n in range(1, 2001)), encoding="utf-8")
    return target, ToolContext(cwd=tmp_path)


def _model_read(tmp_path, monkeypatch, context: ToolContext, args: dict) -> str:
    """One `read` call the way the model's arrives: in a turn, validated
    against the published schema, then dispatched."""
    turn = run(tmp_path, monkeypatch, build_default_registry().select(["read:eager"]),
               [[("1", "read", json.dumps(args))]], cfg=config(tmp_path, max_tool_result_bytes=0),
               context=context)
    assert [event["invalid"] for event in turn.events("tool_call_batch_normalized")] == [0], turn.results()
    return turn.results()["1"]


def _shown_lines(result: str) -> list[int]:
    return [int(line.split("|", 1)[0]) for line in result.splitlines() if "|" in line and line.split("|", 1)[0].isdigit()]


@pytest.mark.parametrize(("extra", "first", "last"), [
    ({"range": "1,110"}, 1, 110),
    ({"range": "170-185"}, 170, 185),
    ({"range": "start_line 170"}, 170, None),
    ({"range": '{"end_line": 1395, "start_line": 1380'}, 1380, 1395),
    ({"range": {"start_line": 170}, "end_line": 185}, 170, 185),
    ({"range": {"start_line": 5, "end_line": 9}}, 5, 9),
    ({"offset": 170, "limit": 16}, 170, 185),
    ({"line_start": 170, "line_end": 185}, 170, 185),
    ({"start_line": 170, "end_line": 185}, 170, 185),
])
def test_read_takes_each_range_shape_models_send(numbered, extra, first, last, tmp_path, monkeypatch):
    target, context = numbered

    result = _model_read(tmp_path, monkeypatch, context, {"file_path": str(target), **extra})

    shown = _shown_lines(result)
    assert shown[0] == first
    if last is not None:
        assert shown[-1] == last


def test_a_line_range_wins_over_a_byte_range_with_a_note(numbered, tmp_path, monkeypatch):
    target, context = numbered

    result = _model_read(tmp_path, monkeypatch, context, {"file_path": str(target), "start_line": 3, "end_line": 4,
                                   "start_byte": 0, "end_byte": 10})

    plain = fs.read(str(target), start_line=3, end_line=4, context=ToolContext(cwd=target.parent))
    assert result.startswith(plain)
    assert result.count("\n") == plain.count("\n") + 1


def test_a_line_range_of_a_file_over_file_bytes_max_is_streamed(tmp_path):
    target = tmp_path / "big.log"
    target.write_text("".join(f"entry {n:07d} " + "x" * 80 + "\n" for n in range(1, 33_001)), encoding="utf-8")
    assert target.stat().st_size > 3_000_000
    context = ToolContext(cwd=tmp_path, max_file_bytes=1_000_000, max_read_bytes=0)

    result = fs.read(str(target), start_line=20_000, end_line=20_010, context=context)

    assert not result.startswith("ERROR"), result[:200]
    shown = _shown_lines(result)
    assert shown == list(range(20_000, 20_011))
    assert f"entry {20_000:07d} " in result


def test_a_whole_read_of_a_file_over_file_bytes_max_names_the_ranged_call(tmp_path):
    target = tmp_path / "big.log"
    target.write_text("line\n" * 1000, encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_file_bytes=100, max_read_bytes=0)

    result = fs.read(str(target), context=context)

    assert result.startswith("ERROR")
    call = json.loads(result[result.index("{"):result.rindex("}") + 1])
    page = fs.read(context=context, **call)
    assert page.splitlines()[0] == "1|line"


def test_a_missing_file_suggests_the_near_name_beside_it(tmp_path):
    (tmp_path / "results.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_jev_tool.py").write_text("x = 1\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    missed = fs.read(str(tmp_path / "result.py"), context=context)
    missed_test = patch.patch(file_path="test_jev.py", old_string="x", new_string="y", context=context)

    assert missed.startswith("ERROR")
    assert "results.py" in missed
    assert str(tmp_path) in missed
    assert missed_test.startswith("ERROR")
    assert "test_jev_tool.py" in missed_test


def test_a_missing_path_suggests_the_existing_path_with_the_same_tail(tmp_path):
    real = tmp_path / "to_spec" / "out" / "moonpie" / "README.md"
    real.parent.mkdir(parents=True)
    real.write_text("# moonpie\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    read_miss = fs.read(str(tmp_path / "out" / "moonpie" / "README.md"), context=context)
    search_miss = file_search.fs_search("moonpie", path=str(tmp_path / "out" / "moonpie"), context=context)

    assert read_miss.startswith("ERROR")
    assert str(real) in read_miss
    assert search_miss.startswith("ERROR")
    assert str(real.parent) in search_miss


def test_a_missing_path_search_skips_vendored_trees(tmp_path):
    vendored = tmp_path / "node_modules" / "pkg" / "index.js"
    vendored.parent.mkdir(parents=True)
    vendored.write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = fs.read(str(tmp_path / "src" / "pkg" / "index.js"), context=context)

    assert result.startswith("ERROR")
    assert str(vendored) not in result
