"""A read that repeats an earlier one, of unchanged content, returns a short
note naming the earlier read call while that call's whole result is still in
the history the model sees. Otherwise the lines come back in full."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from js import compaction, runtime
from js.toolkit import ToolContext, fs
from js.toolkit.core import call_scope
from js.toolkit.registry import build_default_registry
from test_lazy_tool_discovery import _cfg, _result


def _read_call(call_id: str, target, **rng) -> tuple[str, str, str]:
    args: dict = {"file_path": str(target)}
    if rng:
        args["range"] = rng
    return (call_id, "read", json.dumps(args))


def _run(tmp_path, monkeypatch, steps, *, context=None, between=None, **cfg_changes):
    """Run one turn whose model makes the tool-call batches in ``steps``, then
    stops. ``between(messages, n)`` runs before model call ``n``."""
    messages = [{"role": "user", "content": "go"}]
    context = context or ToolContext(cwd=tmp_path)
    results = iter([*(_result(*calls) for calls in steps), _result(text="done")])
    calls = {"n": 0}

    def model(**_kw):
        if between is not None:
            between(messages, calls["n"])
        calls["n"] += 1
        return next(results)

    monkeypatch.setattr(runtime.model_client, "stream_model_async", model)
    cfg = replace(_cfg(tmp_path), max_tool_iterations=len(steps) + 2, **cfg_changes)
    asyncio.run(runtime.run_turn_async(cfg, "system", messages, runtime.Telemetry(None),
                                       tool_registry=build_default_registry(), tool_context=context,
                                       suppress_output=True))
    return messages, context


def _content(messages, call_id: str) -> str:
    return next(m["content"] for m in messages if m.get("tool_call_id") == call_id)


def _file(tmp_path, lines: int = 20):
    target = tmp_path / "notes.txt"
    target.write_text("".join(f"row {n} of the notes file, long enough to outweigh a note\n"
                              for n in range(1, lines + 1)), encoding="utf-8")
    return target


def test_an_unchanged_reread_of_the_same_lines_names_the_earlier_read(tmp_path, monkeypatch):
    target = _file(tmp_path)

    messages, context = _run(tmp_path, monkeypatch, [
        [_read_call("first", target, start_line=3, end_line=8)],
        [_read_call("again", target, start_line=3, end_line=8)],
    ])

    first, again = _content(messages, "first"), _content(messages, "again")
    assert "3|row 3" in first
    assert "first" in again
    assert "row 3" not in again
    assert len(again) < len(first)
    # The stub counts as reading those lines.
    assert context.require_read(target.resolve(), "edit", line_ranges=[(3, 8)]) is None


def test_a_whole_file_reread_is_a_stub_too(tmp_path, monkeypatch):
    target = _file(tmp_path)

    messages, _ = _run(tmp_path, monkeypatch, [
        [_read_call("first", target)],
        [_read_call("again", target)],
    ])

    assert "first" in _content(messages, "again")
    assert "row 1" not in _content(messages, "again")


def test_a_different_range_comes_back_in_full(tmp_path, monkeypatch):
    target = _file(tmp_path)

    messages, _ = _run(tmp_path, monkeypatch, [
        [_read_call("first", target, start_line=3, end_line=8)],
        [_read_call("wider", target, start_line=3, end_line=9)],
        [_read_call("plain", target, start_line=3, end_line=8)],
    ])

    assert "9|row 9" in _content(messages, "wider")
    assert "first" in _content(messages, "plain")


def test_numbered_and_plain_reads_do_not_stand_for_each_other(tmp_path, monkeypatch):
    target = _file(tmp_path)
    plain = ("plain", "read", json.dumps({"file_path": str(target), "show_line_numbers": False}))

    messages, _ = _run(tmp_path, monkeypatch, [
        [_read_call("first", target)],
        [plain],
    ])

    assert _content(messages, "plain").startswith("row 1 of")


def test_a_file_changed_on_disk_comes_back_in_full(tmp_path, monkeypatch):
    target = _file(tmp_path)

    def edit_between(_messages, n):
        if n == 1:
            target.write_text(target.read_text(encoding="utf-8").replace("row 4 of", "row four of"),
                              encoding="utf-8")

    messages, _ = _run(tmp_path, monkeypatch, [
        [_read_call("first", target, start_line=1, end_line=10)],
        [_read_call("again", target, start_line=1, end_line=10)],
    ], between=edit_between)

    assert "4|row four of" in _content(messages, "again")


def test_a_patch_then_undo_back_to_the_same_bytes_reads_in_full(tmp_path, monkeypatch):
    target = _file(tmp_path)
    patch = ("edit", "patch", json.dumps({"file_path": str(target), "old_string": "row 2 of",
                                           "new_string": "row two of"}))
    undo = ("back", "undo", json.dumps({"path": str(target)}))

    messages, _ = _run(tmp_path, monkeypatch, [
        [_read_call("first", target)],
        [patch],
        [undo],
        [_read_call("again", target)],
    ])

    assert _content(messages, "edit").startswith("patched "), _content(messages, "edit")
    assert _content(messages, "back").startswith("restored "), _content(messages, "back")
    assert "2|row 2" in _content(messages, "again")


def test_a_cleared_earlier_result_is_not_named(tmp_path, monkeypatch):
    target = _file(tmp_path)

    def clear_first(messages, n):
        if n == 2:
            for i, message in enumerate(messages):
                if message.get("tool_call_id") == "first":
                    messages[i] = {**message, "content": compaction.MICROCOMPACT_CLEARED_MESSAGE}

    messages, _ = _run(tmp_path, monkeypatch, [
        [_read_call("first", target)],
        [_read_call("second", target)],
        [_read_call("third", target)],
    ], between=clear_first)

    # "second" came before the clearing and names "first"; once "first" is
    # cleared, "second" (a stub) holds no lines either, so "third" is full.
    assert "first" in _content(messages, "second")
    assert "1|row 1" in _content(messages, "third")


def test_a_summarised_away_earlier_result_is_not_named(tmp_path, monkeypatch):
    target = _file(tmp_path)

    def drop_history(messages, n):
        if n == 1:
            messages[:] = [{"role": "user", "content": "summary of earlier work"}]

    messages, _ = _run(tmp_path, monkeypatch, [
        [_read_call("first", target)],
        [_read_call("again", target)],
    ], between=drop_history)

    assert "1|row 1" in _content(messages, "again")


def test_a_clipped_earlier_read_is_not_named(tmp_path, monkeypatch):
    target = tmp_path / "big.txt"
    target.write_text("".join(f"L{n:05d} filler\n" for n in range(1, 2001)), encoding="utf-8")

    messages, _ = _run(tmp_path, monkeypatch, [
        [_read_call("first", target)],
        [_read_call("again", target)],
    ], max_tool_result_bytes=0, max_tool_results_per_turn_bytes=8000,
        max_tool_result_inline_bytes=0, max_read_lines=5000, max_read_bytes=0)

    assert "max_tool_results_per_turn_bytes" in _content(messages, "first")
    assert "first" not in _content(messages, "again").split("\n", 1)[0]
    assert _content(messages, "again").startswith("1|L00001")


def test_two_reads_of_the_same_lines_in_one_batch_both_come_back_in_full(tmp_path, monkeypatch):
    target = _file(tmp_path)

    messages, _ = _run(tmp_path, monkeypatch, [
        [_read_call("a", target), _read_call("b", target, start_line=1, end_line=20)],
    ], max_parallel_tools=8)

    assert "1|row 1" in _content(messages, "a")
    assert "1|row 1" in _content(messages, "b")


def test_a_read_outside_a_tool_call_never_stubs(tmp_path):
    target = _file(tmp_path)
    context = ToolContext(cwd=tmp_path)

    first = fs.read(str(target), context=context)
    context.settle_reads()
    second = fs.read(str(target), context=context)

    assert first == second


def test_the_ledger_forgets_a_read_whose_result_text_changed(tmp_path):
    target = _file(tmp_path)
    context = ToolContext(cwd=tmp_path)
    with call_scope("r1"):
        shown = fs.read(str(target), context=context)
    context.settle_reads()
    context.keep_shown_reads({"r1": shown})
    with call_scope("r2"):
        assert "r1" in fs.read(str(target), context=context)
    context.settle_reads()

    context.keep_shown_reads({"r1": shown[:-1]})
    with call_scope("r3"):
        assert fs.read(str(target), context=context) == shown
