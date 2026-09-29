"""Summary breaker, plain-text summary requests, iterative summaries,
cache-aware tool-result clearing and the prompt-cache break line."""

import asyncio
import re
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import ai
import pytest
from ai.types.usage import Usage

from js import compaction, model_client, runtime, stream_transport
from js import messages as msgs
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry
from test_lazy_tool_discovery import _cfg, _result


@pytest.fixture(autouse=True)
def offline_metadata(monkeypatch):
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)


@pytest.fixture
def net_lines():
    lines: list[str] = []
    level = {"value": 2}
    stream_transport.install_sink(stream_transport.NetSink(level=lambda: level["value"], emit=lines.append))
    try:
        yield lines, level
    finally:
        stream_transport.install_sink(None)


def _config(tmp_path, compact: dict | None = None):
    return replace(_cfg(tmp_path), provider_id="openai", provider_api_key="offline",
                   provider_base_url="http://127.0.0.1:1/v1", max_output_tokens=500,
                   settings={"compact": dict(compact or {})})


# A 10k window with a 500-token reply reserve and a 100-token buffer.
_SMALL_WINDOW = {"context_window": 10000, "tail_tokens": 100, "buffer_tokens": 100,
                 "summary_reserve_tokens": 500}


def _over_budget_history() -> list[dict]:
    return [{"role": "user", "content": "old " * 15000},
            {"role": "assistant", "content": "prior answer"},
            {"role": "user", "content": "continue"}]


def _tool_heavy_history(n: int = 30, body: int = 2000) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "start"}]
    for i in range(n):
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "read", "content": "x" * body},
        ])
    messages.append({"role": "user", "content": "continue"})
    return messages


def _turn(cfg, messages, context):
    asyncio.run(runtime.run_turn_async(
        cfg, "system", messages, runtime.Telemetry(None),
        tool_registry=build_default_registry().select([]),
        tool_context=context, suppress_output=True,
    ))


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# --- breaker -----------------------------------------------------------------


def test_three_failed_summaries_pause_in_turn_compaction_with_one_line(monkeypatch, tmp_path, capsys):
    cfg = _config(tmp_path, _SMALL_WINDOW)
    context = ToolContext(cwd=tmp_path)
    attempts = []

    async def sdk(**kwargs):
        return _result(text="ok")

    async def failing_summary(*args, **kwargs):
        attempts.append(args)
        raise RuntimeError("summarizer down")

    monkeypatch.setattr(model_client, "_stream_async", sdk)
    monkeypatch.setattr(compaction, "summarize", failing_summary)
    for _ in range(4):
        _turn(cfg, _over_budget_history(), context)

    assert len(attempts) == 3
    assert compaction.auto_paused(cfg, context)
    err = _plain(capsys.readouterr().err)
    assert err.count(msgs.AUTO_COMPACT_BREAKER.text(failures=3)) == 1


def test_breaker_limit_is_the_setting(monkeypatch, tmp_path):
    cfg = _config(tmp_path, {**_SMALL_WINDOW, "max_summary_failures": 1})
    context = ToolContext(cwd=tmp_path)
    attempts = []

    async def sdk(**kwargs):
        return _result(text="ok")

    async def failing_summary(*args, **kwargs):
        attempts.append(args)
        raise RuntimeError("summarizer down")

    monkeypatch.setattr(model_client, "_stream_async", sdk)
    monkeypatch.setattr(compaction, "summarize", failing_summary)
    _turn(cfg, _over_budget_history(), context)
    _turn(cfg, _over_budget_history(), context)
    assert len(attempts) == 1


def test_a_successful_summary_resumes_paused_compaction(monkeypatch, tmp_path):
    cfg = _config(tmp_path, _SMALL_WINDOW)
    context = ToolContext(cwd=tmp_path)
    context.summary_failures = 3
    assert compaction.auto_paused(cfg, context)

    async def summary(*args, **kwargs):
        return "short summary"

    monkeypatch.setattr(compaction, "summarize", summary)
    result = compaction.compact_now_sync(cfg, "system", _over_budget_history(), context=context)
    assert compaction.compacted(result)
    assert context.summary_failures == 0
    assert not compaction.auto_paused(cfg, context)


def test_between_turn_compaction_pauses_after_three_failures(monkeypatch, tmp_path):
    cfg = _config(tmp_path, {"context_window": 100, "buffer_tokens": 0})
    context = ToolContext(cwd=tmp_path)
    context.last_prompt_tokens = 95
    failing = AsyncMock(side_effect=RuntimeError("summarizer down"))
    ac = compaction.AutoCompactState()
    notices = []
    monkeypatch.setattr(compaction, "compact_now", failing)
    for _ in range(5):
        out = asyncio.run(compaction.maybe_auto_compact_async(
            cfg, ac, context, "system", [{"role": "user", "content": "hi"}], lambda: 100))
        notices.extend(out.notices)
    assert failing.await_count == 3
    assert [n for n in notices if n.message is msgs.AUTO_COMPACT_BREAKER] == [
        msgs.AUTO_COMPACT_BREAKER.said(failures=3)]


# --- the summary request -----------------------------------------------------


def _capture_summary_requests(monkeypatch, replies):
    requests = []

    async def sdk(**kwargs):
        requests.append(kwargs["messages"][0].parts[0].text)
        return _result(text=replies[len(requests) - 1])

    monkeypatch.setattr(model_client, "_stream_async", sdk)
    return requests


def test_summary_request_is_plain_text_with_tool_results_clipped(monkeypatch, tmp_path):
    requests = _capture_summary_requests(monkeypatch, ["summary"])
    body = "HEAD" + "y" * 10_000 + "TAIL"
    user = 'say "hi"\nthen go on'
    messages = [
        {"role": "user", "content": user},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read", "content": body},
    ]
    asyncio.run(compaction.summarize(_config(tmp_path), "offline-test", messages, "", ""))

    (text,) = requests
    assert user in text                 # verbatim, not JSON-escaped
    assert body not in text
    assert "HEAD" in text and "TAIL" in text
    assert len(text) < 2000 + 2000      # the clipped result plus the instructions


def test_summary_tool_result_chars_zero_sends_whole_results(monkeypatch, tmp_path):
    requests = _capture_summary_requests(monkeypatch, ["summary"])
    body = "z" * 10_000
    messages = [{"role": "user", "content": "go"},
                {"role": "tool", "tool_call_id": "c1", "name": "read", "content": body}]
    cfg = _config(tmp_path, {"summary_tool_result_chars": 0})
    asyncio.run(compaction.summarize(cfg, "offline-test", messages, "", ""))
    assert body in requests[0]


def test_second_compaction_updates_the_previous_summary(monkeypatch, tmp_path):
    requests = _capture_summary_requests(monkeypatch, ["FIRST_SUMMARY", "SECOND_SUMMARY"])
    cfg = _config(tmp_path, {"tail_tokens": 10, "min_savings_tokens": 1})
    context = ToolContext(cwd=tmp_path)
    messages = [{"role": "user", "content": "EARLY_WORK " * 500},
                {"role": "assistant", "content": "done early"},
                {"role": "user", "content": "next"}]
    assert compaction.compacted(compaction.compact_now_sync(cfg, "system", messages, context=context))
    messages.extend([{"role": "assistant", "content": "LATER_WORK " * 500},
                     {"role": "user", "content": "and now"}])
    assert compaction.compacted(compaction.compact_now_sync(cfg, "system", messages, context=context))

    second = requests[1]
    previous = second.split("<previous-summary>\n", 1)[1].split("\n</previous-summary>", 1)[0]
    conversation = second.split("<conversation>\n", 1)[1].split("\n</conversation>", 1)[0]
    assert previous == "FIRST_SUMMARY"
    assert "FIRST_SUMMARY" not in conversation
    assert "LATER_WORK" in conversation
    summaries = [m for m in messages if str(m.get("content", "")).startswith("<compaction-summary>")]
    assert len(summaries) == 1 and "SECOND_SUMMARY" in summaries[0]["content"]


def test_split_summary_sends_the_previous_summary_once(monkeypatch, tmp_path):
    requests = []
    overflow = ai.ProviderAPIError("context_length_exceeded", provider="openai")

    async def sdk(**kwargs):
        requests.append(kwargs["messages"][0].parts[0].text)
        if len(requests) == 1:
            raise overflow
        return _result(text=f"part {len(requests)}")

    monkeypatch.setattr(model_client, "_stream_async", sdk)
    messages = [compaction._compaction_summary_message("OLD_SUMMARY"),
                *({"role": "user", "content": f"entry {i}"} for i in range(8))]
    summary = asyncio.run(compaction.summarize(_config(tmp_path), "offline-test", messages, "", ""))
    assert ["OLD_SUMMARY" in r for r in requests] == [True, True, False]
    assert "part 2" in summary and "part 3" in summary


# --- cache-aware clearing ------------------------------------------------------


@pytest.mark.parametrize("age", [None, 10_000])
def test_budget_clears_tool_results_once_the_cache_expired(monkeypatch, tmp_path, age):
    cfg = _config(tmp_path, _SMALL_WINDOW)
    context = ToolContext(cwd=tmp_path)
    context.last_request_at = None if age is None else time.time() - age
    summaries = []

    async def sdk(**kwargs):
        return _result(text="ok")

    async def summary(*args, **kwargs):
        summaries.append(args)
        return "summary"

    monkeypatch.setattr(model_client, "_stream_async", sdk)
    monkeypatch.setattr(compaction, "summarize", summary)
    messages = _tool_heavy_history()
    _turn(cfg, messages, context)
    assert any(m.get("content") == compaction.MICROCOMPACT_CLEARED_MESSAGE for m in messages)
    assert summaries == []


def test_budget_summarizes_instead_of_clearing_while_the_cache_is_warm(monkeypatch, tmp_path):
    cfg = _config(tmp_path, _SMALL_WINDOW)
    context = ToolContext(cwd=tmp_path)
    context.last_request_at = time.time() - 30
    summaries = []

    async def sdk(**kwargs):
        return _result(text="ok")

    async def summary(*args, **kwargs):
        summaries.append(args)
        return "summary"

    monkeypatch.setattr(model_client, "_stream_async", sdk)
    monkeypatch.setattr(compaction, "summarize", summary)
    messages = _tool_heavy_history()
    _turn(cfg, messages, context)
    assert summaries
    assert not any(m.get("content") == compaction.MICROCOMPACT_CLEARED_MESSAGE for m in messages)


def test_cache_ttl_is_the_setting(tmp_path):
    context = ToolContext(cwd=tmp_path)
    context.last_request_at = 1000.0
    assert not compaction.cache_expired(_config(tmp_path), context, now=1299.0)
    assert compaction.cache_expired(_config(tmp_path), context, now=1300.0)
    assert compaction.cache_expired(_config(tmp_path, {"cache_ttl_seconds": 0}), context, now=1000.0)
    assert not compaction.cache_expired(_config(tmp_path, {"cache_ttl_seconds": 3600}), context, now=1300.0)


def test_provider_overflow_clears_even_while_the_cache_is_warm(monkeypatch, tmp_path):
    cfg = _config(tmp_path)
    context = ToolContext(cwd=tmp_path)
    context.last_request_at = time.time()
    calls = []

    async def sdk(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ai.ProviderAPIError("context_length_exceeded", provider="openai")
        return _result(text="ok")

    monkeypatch.setattr(model_client, "_stream_async", sdk)
    messages = _tool_heavy_history(n=22)
    _turn(cfg, messages, context)
    assert any(m.get("content") == compaction.MICROCOMPACT_CLEARED_MESSAGE for m in messages)
    assert messages[-1]["content"] == "ok"


# --- the cache-break line ------------------------------------------------------


def test_note_response_reports_a_cache_read_drop_over_five_percent(tmp_path):
    context = ToolContext(cwd=tmp_path)
    assert compaction.note_response(context, model_key="p/m", cache_read=100_000, now=0.0) is None
    assert compaction.note_response(context, model_key="p/m", cache_read=96_000, now=1.0) is None
    line = compaction.note_response(context, model_key="p/m", cache_read=40_000, now=2.0)
    assert line is not None and "96000" in line and "40000" in line


def test_note_response_compares_only_comparable_requests(tmp_path):
    context = ToolContext(cwd=tmp_path)
    compaction.note_response(context, model_key="p/m", cache_read=100_000, now=0.0)
    assert compaction.note_response(context, model_key="p/other", cache_read=10_000, now=1.0) is None
    compaction.note_response(context, model_key="p/m", cache_read=100_000, now=2.0)
    compaction.history_rewritten(context)
    assert compaction.note_response(context, model_key="p/m", cache_read=10_000, now=3.0) is None


def test_note_response_ignores_drops_under_the_token_floor(tmp_path):
    context = ToolContext(cwd=tmp_path)
    compaction.note_response(context, model_key="p/m", cache_read=1_000, now=0.0)
    assert compaction.note_response(context, model_key="p/m", cache_read=100, now=1.0) is None


def _cached_turns(monkeypatch, tmp_path, cache_reads):
    reads = iter(cache_reads)

    async def sdk(**kwargs):
        return replace(_result(text="ok"), usage=Usage(input_tokens=120_000, cache_read_tokens=next(reads),
                                                        output_tokens=1))

    monkeypatch.setattr(model_client, "_stream_async", sdk)
    cfg = _config(tmp_path)
    context = ToolContext(cwd=tmp_path)
    messages = [{"role": "user", "content": "hi"}]
    for _ in cache_reads:
        _turn(cfg, messages, context)
        messages.append({"role": "user", "content": "again"})


def test_cache_break_prints_one_line_at_net_level_two(monkeypatch, tmp_path, net_lines):
    lines, level = net_lines
    _cached_turns(monkeypatch, tmp_path, [100_000, 100_000, 20_000])
    breaks = [_plain(line) for line in lines if "100000" in _plain(line) and "20000" in _plain(line)]
    assert len(breaks) == 1


def test_cache_break_is_silent_below_net_level_two(monkeypatch, tmp_path, net_lines):
    lines, level = net_lines
    level["value"] = 1
    _cached_turns(monkeypatch, tmp_path, [100_000, 20_000])
    assert not any("20000" in _plain(line) for line in lines)
