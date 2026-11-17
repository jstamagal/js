"""What the model receives as a tool call's result, through `runtime.run_turn`.

A tool runs one of three ways: a sync handler on a worker thread, an async
handler on the loop, or a fan-out handler (task / named agent) awaited on the
loop. Each case below runs once per way.
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from js import runtime
from js.text_bytes import byte_size
from js.toolkit import meta
from js.toolkit.core import Tool, ToolContext
from js.toolkit.registry import ToolRegistry
from test_lazy_tool_discovery import _cfg, _result

KINDS = ("sync", "async", "fan_out")
RETRY = re.compile(r"<retry>attempts_left=(\d+), allowed_max_attempts=(\d+)</retry>$")
CAP = 300


def _behave(mode: str) -> str:
    if mode == "raise":
        raise ValueError("boom")
    if mode == "error":
        return "ERROR: probe failed on /tmp/x at line 3"
    if mode == "big":
        return "x" * 2000
    if mode == "big_error":
        return "ERROR: " + "y" * 2000
    return "fine"


def _registry(kind: str, monkeypatch, ran: list[str]) -> ToolRegistry:
    def sync_probe(mode: str = "ok") -> str:
        ran.append(mode)
        return _behave(mode)

    async def async_probe(mode: str = "ok") -> str:
        ran.append(mode)
        return _behave(mode)

    params = {"mode": {"type": "string"}}
    if kind == "async":
        return ToolRegistry((Tool("probe", "Probe.", async_probe, params),), {})
    if kind == "fan_out":
        sync_probe._js_fan_out = True

        async def dispatch_fan_out(tool, args, context):
            return tool.handler(**args)

        monkeypatch.setattr(meta, "dispatch_fan_out_async", dispatch_fan_out)
    return ToolRegistry((Tool("probe", "Probe.", sync_probe, params),), {})


def _run(tmp_path, monkeypatch, registry: ToolRegistry, batches: list[list[tuple[str, str, str]]],
         cfg=None) -> tuple[list[dict[str, object]], list[dict]]:
    """Run one turn whose model makes ``batches`` of calls, then answers.
    Returns, per batch, the results the next model call received by call id,
    and the turn's history."""
    seen: list[list] = []

    async def model(**kwargs):
        seen.append(list(kwargs["messages"]))
        index = len(seen) - 1
        return _result(*batches[index]) if index < len(batches) else _result(text="done")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", model)
    messages = [{"role": "user", "content": "go"}]
    runtime.run_turn(cfg or _cfg(tmp_path), "SYS", messages, runtime.Telemetry(debug_log=None),
                     tool_registry=registry, tool_context=ToolContext(cwd=tmp_path), suppress_output=True)
    received = [
        {part.tool_call_id: part.result
         for message in convo for part in message.parts if part.kind == "tool_result"}
        for convo in seen[1:]
    ]
    return received, messages


def _one(tmp_path, monkeypatch, kind: str, args: str, cfg=None) -> tuple[str, list[str]]:
    ran: list[str] = []
    received, _ = _run(tmp_path, monkeypatch, _registry(kind, monkeypatch, ran),
                       [[("c1", "probe", args)]], cfg)
    return received[0]["c1"], ran


def _retry(result: str) -> tuple[int, int] | None:
    match = RETRY.search(result)
    return (int(match[1]), int(match[2])) if match else None


@pytest.mark.parametrize("kind", KINDS)
def test_a_successful_call_reaches_the_model_unchanged(tmp_path, monkeypatch, kind):
    result, ran = _one(tmp_path, monkeypatch, kind, '{"mode":"ok"}')
    assert ran == ["ok"]
    assert result == "fine"


@pytest.mark.parametrize("kind", KINDS)
def test_a_tool_that_raises_reaches_the_model_as_a_tracked_error(tmp_path, monkeypatch, kind):
    result, ran = _one(tmp_path, monkeypatch, kind, '{"mode":"raise"}')
    assert ran == ["raise"]
    assert result.startswith("ERROR")
    assert "ValueError: boom" in result
    assert _retry(result) == (2, 3)


@pytest.mark.parametrize("kind", KINDS)
def test_an_error_result_carries_the_retry_note(tmp_path, monkeypatch, kind):
    result, _ = _one(tmp_path, monkeypatch, kind, '{"mode":"error"}')
    assert result.startswith(_behave("error") + "\n")
    assert _retry(result) == (2, 3)


@pytest.mark.parametrize("kind", KINDS)
def test_malformed_arguments_are_a_tracked_error_and_the_tool_never_runs(tmp_path, monkeypatch, kind):
    result, ran = _one(tmp_path, monkeypatch, kind, "not json")
    assert ran == []
    assert result.startswith("ERROR")
    assert _retry(result) == (2, 3)


def test_an_unknown_tool_is_an_error_the_tracker_does_not_count(tmp_path, monkeypatch):
    ran: list[str] = []
    batches = [[(f"c{n}", "nope", "{}")] for n in range(1, 5)]
    _, messages = _run(tmp_path, monkeypatch, _registry("sync", monkeypatch, ran), batches)
    results = [message["content"] for message in messages if message["role"] == "tool"]
    assert ran == []
    assert len(results) == 4
    assert all(result.startswith("ERROR") and _retry(result) is None for result in results)
    assert not any(message.get("steered") for message in messages)


@pytest.mark.parametrize("kind", KINDS)
def test_an_oversized_result_is_cut_to_the_result_cap(tmp_path, monkeypatch, kind):
    cfg = replace(_cfg(tmp_path), max_tool_result_bytes=CAP)
    result, _ = _one(tmp_path, monkeypatch, kind, '{"mode":"big"}', cfg)
    assert result.startswith("x" * 100)
    assert byte_size(result) <= CAP


@pytest.mark.parametrize("kind", KINDS)
def test_an_oversized_error_is_counted_whole_then_cut_to_the_result_cap(tmp_path, monkeypatch, kind):
    cfg = replace(_cfg(tmp_path), max_tool_result_bytes=CAP)
    result, _ = _one(tmp_path, monkeypatch, kind, '{"mode":"big_error"}', cfg)
    assert result.startswith("ERROR: " + "y" * 100)
    # The retry note is cut off with the tail.
    assert byte_size(result) <= CAP
    assert _retry(result) is None


@pytest.mark.parametrize("kind", KINDS)
def test_a_repeated_error_counts_down_then_steers_then_ends_the_turn(tmp_path, monkeypatch, kind):
    ran: list[str] = []
    batches = [[(f"c{n}", "probe", '{"mode":"error"}')] for n in range(1, 6)]
    received, messages = _run(tmp_path, monkeypatch, _registry(kind, monkeypatch, ran), batches)
    assert len(ran) == 4
    results = {message["tool_call_id"]: message["content"] for message in messages if message["role"] == "tool"}
    assert [_retry(result) for result in results.values()] == [(2, 3), (1, 3), (0, 3), (0, 3)]
    # The model received each result before its next call.
    assert all(received[n][f"c{n + 1}"] == results[f"c{n + 1}"] for n in range(3))
    steered = [index for index, message in enumerate(messages) if message.get("steered")]
    assert len(steered) == 1
    # The steer follows the third result and precedes the fourth call.
    assert messages[steered[0] - 1]["tool_call_id"] == "c3"
    assert messages[steered[0] + 1].get("tool_calls")[0]["id"] == "c4"
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"].startswith("ERROR")
    assert _behave("error") in messages[-1]["content"]
