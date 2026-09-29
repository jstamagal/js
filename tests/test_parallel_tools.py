"""Read-only calls of one assistant batch run together; a call that writes runs
alone, after every call before it and before every call after it."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from js import display, runtime, settings
from js.toolkit import core as tool_core, fs
from js.toolkit.core import Tool, ToolContext, call_is_read_only
from js.toolkit.registry import ToolRegistry, build_default_registry
from test_lazy_tool_discovery import _cfg, _result


class Probe:
    """Counts the calls running at once and remembers the order calls end in."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = 0
        self.most = 0
        self.finished: list[str] = []

    def enter(self) -> None:
        with self.lock:
            self.running += 1
            self.most = max(self.most, self.running)

    def leave(self, label: str) -> None:
        with self.lock:
            self.running -= 1
            self.finished.append(label)


def _sleeper_registry(probe: Probe, seconds: float, *, read_only: bool = True) -> ToolRegistry:
    def sleeper(label: str, context: ToolContext | None = None) -> str:
        probe.enter()
        time.sleep(seconds)
        probe.leave(label)
        return f"slept {label}"

    return ToolRegistry((Tool("sleeper", "test", sleeper, {"label": {"type": "string"}},
                              required=("label",), read_only=read_only),), {})


def _calls(*labels: str, name: str = "sleeper") -> list[runtime._PendingToolCall]:
    return [runtime._PendingToolCall(label, name, [json.dumps({"label": label})]) for label in labels]


def _dispatch(calls, registry, context, *, trace: bool = False, progress=None):
    return runtime._dispatch_tool_calls(
        calls, runtime.Telemetry(None), 256 * 1024, trace, runtime.ToolErrorTracker(),
        registry, context, progress,
    )


def test_three_read_only_calls_that_each_sleep_half_a_second_run_at_once(tmp_path):
    probe = Probe()
    context = ToolContext(cwd=tmp_path, max_parallel_tools=8)

    started = time.perf_counter()
    records = _dispatch(_calls("a", "b", "c"), _sleeper_registry(probe, 0.5), context)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0
    assert probe.most == 3
    assert [result for _pc, _args, result in records] == ["slept a", "slept b", "slept c"]


def test_results_come_back_in_the_model_order_not_the_finish_order(tmp_path):
    probe = Probe()
    finish_first = threading.Event()

    def ordered(label: str, context: ToolContext | None = None) -> str:
        probe.enter()
        if label == "slow":
            assert finish_first.wait(5)
        else:
            finish_first.set()
        probe.leave(label)
        return label.upper()

    registry = ToolRegistry((Tool("sleeper", "test", ordered, {"label": {"type": "string"}},
                                  read_only=True),), {})

    records = _dispatch(_calls("slow", "fast"), registry, ToolContext(cwd=tmp_path, max_parallel_tools=8))

    assert probe.finished == ["fast", "slow"]
    assert [(pc.id, result) for pc, _args, result in records] == [("slow", "SLOW"), ("fast", "FAST")]


def test_max_parallel_tools_one_runs_every_call_in_turn(tmp_path, monkeypatch):
    jsrc = settings.PACKAGE_JSRC.read_text(encoding="utf-8")
    assert re.search(r"^set runtime\.max_parallel_tools 8$", jsrc, re.M)
    copy = tmp_path / "pkg" / "jsrc"
    copy.parent.mkdir()
    copy.write_text(re.sub(r"^set runtime\.max_parallel_tools \d+$",
                           "set runtime.max_parallel_tools 1", jsrc, flags=re.M), encoding="utf-8")
    monkeypatch.setattr(settings, "PACKAGE_JSRC", copy)
    probe = Probe()
    context = ToolContext(cwd=tmp_path)
    assert context.max_parallel_tools == 1

    _dispatch(_calls("a", "b", "c"), _sleeper_registry(probe, 0.05), context)

    assert probe.most == 1
    assert probe.finished == ["a", "b", "c"]


def test_the_cap_bounds_how_many_read_only_calls_run_at_once(tmp_path):
    probe = Probe()

    _dispatch(_calls(*"abcdef"), _sleeper_registry(probe, 0.1), ToolContext(cwd=tmp_path, max_parallel_tools=2))

    assert probe.most == 2


def test_read_read_patch_read_runs_the_reads_together_and_the_patch_alone(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    stock = build_default_registry()
    reads_running = 0
    most_reads = 0
    patch_saw: list[int] = []
    lock = threading.Lock()

    def read(context=None, **kwargs):
        nonlocal reads_running, most_reads
        with lock:
            reads_running += 1
            most_reads = max(most_reads, reads_running)
        try:
            time.sleep(0.2)
            return fs.fs_read(context=context, **kwargs)
        finally:
            with lock:
                reads_running -= 1

    def patch(context=None, **kwargs):
        patch_saw.append(reads_running)
        time.sleep(0.2)
        patch_saw.append(reads_running)
        return fs.patch(context=context, **kwargs)

    registry = ToolRegistry((
        dataclasses.replace(stock.resolve("read"), handler=read),
        dataclasses.replace(stock.resolve("patch"), handler=patch),
    ), {})
    read_args = json.dumps({"file_path": str(target)})
    calls = [
        runtime._PendingToolCall("r1", "read", [read_args]),
        runtime._PendingToolCall("r2", "read", [read_args]),
        runtime._PendingToolCall("p1", "patch", [json.dumps(
            {"file_path": str(target), "old_string": "beta", "new_string": "BETA"})]),
        runtime._PendingToolCall("r3", "read", [read_args]),
    ]

    records = _dispatch(calls, registry, ToolContext(cwd=tmp_path, max_parallel_tools=8))

    results = {pc.id: result for pc, _args, result in records}
    assert [pc.id for pc, _args, _result in records] == ["r1", "r2", "p1", "r3"]
    assert most_reads == 2
    assert patch_saw == [0, 0]
    assert "BETA" not in results["r1"] and "BETA" not in results["r2"]
    assert results["p1"].startswith("patched ")
    assert "BETA" in results["r3"]


def test_concurrent_reads_of_one_file_leave_consistent_coverage(tmp_path):
    target = tmp_path / "long.txt"
    target.write_text("".join(f"line {n}\n" for n in range(1, 81)), encoding="utf-8")
    registry = build_default_registry()
    for _round in range(5):
        context = ToolContext(cwd=tmp_path, max_parallel_tools=8)
        calls = [
            runtime._PendingToolCall(f"r{start}", "read", [json.dumps(
                {"file_path": str(target), "range": {"start_line": start, "end_line": start + 9}})])
            for start in range(1, 81, 10)
        ]

        _dispatch(calls, registry, context)

        path = target.resolve()
        assert context.read_ranges[path] == [(1, 80)]
        assert path in context.fully_read_paths
        assert fs.patch(file_path=str(target), old_string="line 55\n", new_string="line 55!\n",
                        context=context).startswith("patched ")
        target.write_text("".join(f"line {n}\n" for n in range(1, 81)), encoding="utf-8")


def test_a_clip_of_one_parallel_read_keeps_the_other_reads_coverage(tmp_path, monkeypatch):
    """Two reads of one file run at once and the per-turn cap clips the whole-file
    one. The ranged read's lines stay read; the clipped tail does not."""
    target = tmp_path / "big.txt"
    target.write_text("".join(f"L{n:05d} filler\n" for n in range(1, 3001)), encoding="utf-8")
    messages = [{"role": "user", "content": "read"}]
    context = ToolContext(cwd=tmp_path)
    results = iter([
        _result(
            ("whole", "read", json.dumps({"file_path": str(target)})),
            ("tail", "read", json.dumps({"file_path": str(target), "range": {"start_line": 2990, "end_line": 3000}})),
        ),
        _result(text="done"),
    ])
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kw: next(results))
    cfg = replace(_cfg(tmp_path), max_tool_result_bytes=0, max_tool_results_per_turn_bytes=8000,
                  max_tool_result_inline_bytes=0, max_read_lines=5000, max_read_bytes=0,
                  max_parallel_tools=8)

    asyncio.run(runtime.run_turn_async(cfg, "system", messages, runtime.Telemetry(None),
                                       tool_registry=build_default_registry(), tool_context=context,
                                       suppress_output=True))

    whole = next(m["content"] for m in messages if m.get("tool_call_id") == "whole")
    assert "max_tool_results_per_turn_bytes" in whole
    path = target.resolve()
    assert path not in context.fully_read_paths
    assert context.require_read(path, "edit", line_ranges=[(2995, 2995)]) is None
    assert context.require_read(path, "edit", line_ranges=[(1500, 1500)]) is not None
    assert context.require_read(path, "edit", line_ranges=[(1, 1)]) is None


def test_cancel_mid_batch_keeps_finished_results_and_starts_nothing_new(tmp_path, monkeypatch):
    messages = [{"role": "user", "content": "run"}]
    ran: list[int] = []
    both_running = threading.Barrier(3)
    release = threading.Event()

    def look(n: int, context=None) -> str:
        ran.append(n)
        if n in (1, 2):
            both_running.wait(3)
            assert release.wait(3)
        return f"look {n}"

    def change(n: int, context=None) -> str:
        ran.append(n)
        return f"change {n}"

    registry = ToolRegistry((
        Tool("look", "test", look, {"n": {"type": "integer"}}, read_only=True),
        Tool("change", "test", change, {"n": {"type": "integer"}}),
    ), {})
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kw: _result(
        ("c1", "look", '{"n":1}'), ("c2", "look", '{"n":2}'), ("c3", "look", '{"n":3}'),
        ("c4", "change", '{"n":4}')))
    cfg = replace(_cfg(tmp_path), max_parallel_tools=2)

    async def run():
        job = asyncio.create_task(runtime.run_turn_async(
            cfg, "system", messages, runtime.Telemetry(None),
            tool_registry=registry, tool_context=ToolContext(cwd=tmp_path), suppress_output=True))
        await asyncio.get_running_loop().run_in_executor(None, both_running.wait, 3)
        job.cancel()
        # The loop keeps running while the cancelled batch drains.
        ticks = 0
        timer = threading.Timer(0.1, release.set)
        timer.start()
        while not job.done():
            ticks += 1
            await asyncio.sleep(0.005)
        timer.join()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert ticks > 5

    asyncio.run(run())
    assert sorted(ran) == [1, 2]
    answered = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
    assert answered == {"c1": "look 1", "c2": "look 2"}


def test_parallel_exchanges_print_whole(capsys, tmp_path):
    probe = Probe()
    context = ToolContext(cwd=tmp_path, max_parallel_tools=8)
    context.config = SimpleNamespace(settings={"ui": {"tools": 2, "tools_preview_lines": 3}})

    def lines(label: str, context=None) -> str:
        probe.enter()
        time.sleep(0.05)
        probe.leave(label)
        return "\n".join(f"{label} row {n}" for n in range(3))

    registry = ToolRegistry((Tool("sleeper", "test", lines, {"label": {"type": "string"}},
                                  read_only=True),), {})

    _dispatch(_calls("a", "b", "c", "d"), registry, context, trace=True)

    assert probe.most > 1
    out = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in capsys.readouterr().out.splitlines()]
    headers = [i for i, line in enumerate(out) if line.startswith(display.TOOL_MARKER + " ")]
    assert len(headers) == 4
    for start, end in zip(headers, [*headers[1:], len(out)], strict=True):
        label = re.search(r'"label":\s*"(\w)"', out[start]) or re.search(r"\b([abcd])\b", out[start])
        assert label is not None, out[start]
        body = out[start + 1:end]
        assert all(row.split()[0] == label.group(1) for row in body if " row " in row), body
        assert sum(" row " in row for row in body) == 3


def test_which_calls_are_read_only():
    registry = build_default_registry()

    def ro(name: str, **args) -> bool:
        return call_is_read_only(registry.resolve(name), args)

    for name in ("read", "fs_search", "skill", "docs_search", "exa_search", "serper_search", "tavily_search"):
        assert ro(name), name
    assert ro("ast_search", pattern="f($A)")
    assert ro("ast_search", pattern="f($A)", rewrite="g($A)")
    assert not ro("ast_search", pattern="f($A)", rewrite="g($A)", apply=True)
    assert ro("fetch", url="https://example.com")
    assert ro("fetch", url="https://example.com", method="head")
    assert not ro("fetch", url="https://example.com", method="POST")
    assert not ro("fetch", url="https://example.com", save="page.html")
    assert ro("browse", url="https://example.com")
    assert not ro("browse", url="https://example.com", screenshot="shot.png")
    for name in ("write", "patch", "remove", "undo", "shell", "kernel", "terminal_session",
                 "terminal_snapshot", "toolbox", "task", "plan", "browser_probe"):
        assert not ro(name), name


def test_a_call_id_scopes_read_coverage_to_its_call(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("".join(f"row {n}\n" for n in range(1, 21)), encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    with tool_core.call_scope("one"):
        first = fs.fs_read(file_path=str(target), start_line=1, end_line=5, context=context)
    with tool_core.call_scope("two"):
        second = fs.fs_read(file_path=str(target), context=context)

    context.record_delivered_read(target.resolve(), second, second.split("\n", 3)[0], "two")

    assert "row 5" in first
    assert context.read_ranges[target.resolve()] == [(1, 5)]
    assert target.resolve() not in context.fully_read_paths


def test_spilled_parallel_results_are_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.paths, "tool_results_dir", lambda: tmp_path / "spill")
    text = "".join(f"{n:06d}\n" for n in range(20000))

    notices = []

    def spill() -> None:
        notices.append(runtime.spill_oversized_result(text, 1000))

    threads = [threading.Thread(target=spill) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    files = list(Path(tmp_path / "spill").iterdir())
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == text
    assert len(set(notices)) == 1
