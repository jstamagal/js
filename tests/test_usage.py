"""Session usage: tokens and cost per model call, totals that survive resume,
/cost, and the status bar's session figure (js-1g1.19)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import ai
import ai.types.usage
import pytest

from js import cli, compaction, runtime, screen, usage
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit.core import ToolContext
from js.toolkit.registry import build_default_registry
from test_lazy_tool_discovery import _cfg


@pytest.fixture(autouse=True)
def fresh_totals(monkeypatch):
    monkeypatch.setattr(usage, "_TOTALS", {})


def _usage(input_tokens=0, output_tokens=0, cache_read=None, cache_write=None, reasoning=None, raw=None):
    return ai.types.usage.Usage(input_tokens=input_tokens, output_tokens=output_tokens,
                                cache_read_tokens=cache_read, cache_write_tokens=cache_write,
                                reasoning_tokens=reasoning, raw=raw)


def _reply(text="", calls=(), use=None):
    tool_calls = [ModelToolCall(id=call_id, name=name, arguments=args) for call_id, name, args in calls]
    parts: list = [ai.types.messages.ToolCallPart(tool_call_id=c.id, tool_name=c.name, tool_args=c.arguments)
                   for c in tool_calls]
    if text or not parts:
        parts.append(ai.types.messages.TextPart(text=text))
    return ModelStreamResult(
        text=text, tool_calls=tool_calls, reasoning="", usage=use,
        finish_reason="tool_calls" if tool_calls else "stop",
        assistant_message=ai.messages.Message(role="assistant", parts=parts),
    )


def _run(cfg, monkeypatch, replies, context=None):
    replies = iter(replies)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kw: next(replies))
    messages = [{"role": "user", "content": "go"}]
    asyncio.run(runtime.run_turn_async(cfg, "system", messages, runtime.Telemetry(None),
                                       tool_registry=build_default_registry(),
                                       tool_context=context or ToolContext(cwd=cfg.session_file.parent),
                                       suppress_output=True))
    return messages


def _priced_cfg(tmp_path):
    cfg = replace(_cfg(tmp_path), model="gpt-5", provider_id="openai")
    cfg.session_file.parent.mkdir(parents=True, exist_ok=True)
    return cfg


# --- one call ----------------------------------------------------------------------

def test_a_priced_call_costs_fresh_input_cached_input_and_output_at_their_rates():
    # models.dev prices openai:gpt-5 at $1.25 in, $0.125 cache read, $10 out per million.
    call = usage.call_usage(_usage(1_000_000, 100_000, cache_read=400_000), model="gpt-5", provider_id="openai")

    assert (call.input_tokens, call.output_tokens, call.cache_read_tokens) == (1_000_000, 100_000, 400_000)
    assert call.cost == pytest.approx(0.6 * 1.25 + 0.4 * 0.125 + 0.1 * 10)


def test_anthropic_cache_writes_count_as_input_and_cost_the_write_rate():
    # The SDK's Anthropic usage leaves cache writes out of input_tokens.
    raw = {"input_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 2000}
    call = usage.call_usage(_usage(1000, 0, cache_read=0, cache_write=2000, raw=raw),
                            model="claude-sonnet-4-5", provider_id="anthropic")

    assert call.input_tokens == 3000
    assert call.cache_write_tokens == 2000
    assert call.cost == pytest.approx((1000 * 3.0 + 2000 * 3.75) / 1_000_000)


def test_a_model_the_catalog_does_not_price_counts_tokens_only():
    call = usage.call_usage(_usage(500, 20), model="qwen3:32b", provider_id="ollama")

    assert (call.input_tokens, call.output_tokens) == (500, 20)
    assert call.cost is None


def test_a_price_tier_applies_from_its_minimum_context():
    tiers = [type("T", (), dict(min_context=0, input=1.0, output=2.0, cache_read=None, cache_write=None)),
             type("T", (), dict(min_context=200_000, input=10.0, output=20.0, cache_read=None, cache_write=None))]
    tokens = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}

    assert usage.call_cost({**tokens, "input_tokens": 1_000_000}, tiers) == pytest.approx(10.0)
    assert usage.call_cost({**tokens, "input_tokens": 100_000}, tiers) == pytest.approx(0.1)


# --- session totals ----------------------------------------------------------------

def test_every_model_call_of_a_turn_adds_to_the_session_totals(tmp_path, monkeypatch):
    cfg = _priced_cfg(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("x\n", encoding="utf-8")
    _run(cfg, monkeypatch, [
        _reply(calls=[("c1", "read", json.dumps({"file_path": str(target)}))],
               use=_usage(1000, 50, cache_read=200, reasoning=10)),
        _reply(text="done", use=_usage(3000, 70, cache_read=1000)),
    ])

    live = usage.totals(cfg.session_file)
    assert live.calls == 2
    assert (live.input_tokens, live.output_tokens) == (4000, 120)
    assert (live.cache_read_tokens, live.reasoning_tokens) == (1200, 10)
    expected = ((800 + 2000) * 1.25 + 1200 * 0.125 + 120 * 10) / 1_000_000
    assert live.cost == pytest.approx(expected)
    assert live.by_model["gpt-5"].calls == 2


def test_totals_survive_resume(tmp_path, monkeypatch):
    cfg = _priced_cfg(tmp_path)
    _run(cfg, monkeypatch, [_reply(text="one", use=_usage(100, 10))])
    before = usage.totals(cfg.session_file).as_dict()

    usage.forget(cfg.session_file)  # a new process knows only the file
    assert usage.totals(cfg.session_file).as_dict() == before

    _run(cfg, monkeypatch, [_reply(text="two", use=_usage(200, 20))])
    usage.forget(cfg.session_file)
    resumed = usage.totals(cfg.session_file)
    assert (resumed.calls, resumed.input_tokens, resumed.output_tokens) == (2, 300, 30)


def test_usage_records_stay_off_the_conversation(tmp_path, monkeypatch):
    from js import memory

    cfg = _priced_cfg(tmp_path)
    messages = _run(cfg, monkeypatch, [_reply(text="hi", use=_usage(100, 10))])
    memory.persist_messages(cfg.session_file, messages)

    assert memory.load_messages(cfg.session_file) == messages
    records = [json.loads(line) for line in cfg.session_file.read_text().splitlines()]
    assert [r["kind"] for r in records].count("usage") == 1
    first_message = next(r for r in records if r["kind"] == "message")
    assert first_message["parent"] is None


def test_a_call_with_no_reported_usage_counts_as_an_unpriced_call(tmp_path, monkeypatch):
    cfg = _priced_cfg(tmp_path)
    _run(cfg, monkeypatch, [_reply(text="hi", use=None)])

    live = usage.totals(cfg.session_file)
    assert (live.calls, live.unpriced_calls, live.input_tokens) == (1, 1, 0)


def test_a_task_worker_call_is_charged_to_its_parent_session_too(tmp_path, monkeypatch):
    cfg = _priced_cfg(tmp_path)
    parent_file = tmp_path / "parent.jsonl"
    context = ToolContext(cwd=tmp_path, usage_chain=(parent_file,))
    _run(cfg, monkeypatch, [_reply(text="hi", use=_usage(100, 10))], context=context)

    usage.forget(parent_file)
    assert usage.totals(parent_file).input_tokens == 100
    assert usage.totals(cfg.session_file).input_tokens == 100


def test_compaction_summary_calls_are_charged_to_the_session(tmp_path, monkeypatch):
    cfg = _priced_cfg(tmp_path)

    async def summary(**kw):
        return _reply(text="## Goal\nsummary", use=_usage(5000, 300))

    monkeypatch.setattr(compaction.model_client, "stream_model_async", summary)
    asyncio.run(compaction.summarize(cfg, "gpt-5", [{"role": "user", "content": "x"}], "", ""))

    assert usage.totals(cfg.session_file).input_tokens == 5000


# --- /cost and the status bar -------------------------------------------------------

def test_cost_command_prints_the_totals(tmp_path, monkeypatch, capsys):
    cfg = _priced_cfg(tmp_path)
    _run(cfg, monkeypatch, [_reply(text="hi", use=_usage(12_345, 678, cache_read=2_000, cache_write=0))])
    live = usage.totals(cfg.session_file)

    cli._cmd_cost("", {}, cfg)

    out = capsys.readouterr().out
    for figure in ("12,345", "678", "2,000", usage.format_dollars(live.cost), "gpt-5"):
        assert figure in out


def test_cost_command_shows_tokens_only_for_an_unpriced_model(tmp_path, monkeypatch, capsys):
    cfg = replace(_cfg(tmp_path), model="qwen3:32b", provider_id="ollama")
    cfg.session_file.parent.mkdir(parents=True, exist_ok=True)
    _run(cfg, monkeypatch, [_reply(text="hi", use=_usage(4_321, 99))])

    cli._cmd_cost("", {}, cfg)

    out = capsys.readouterr().out
    assert "4,321" in out and "99" in out
    assert "$" not in out


def test_status_bar_shows_the_session_cost(tmp_path, monkeypatch):
    cfg = _priced_cfg(tmp_path)
    _run(cfg, monkeypatch, [_reply(text="hi", use=_usage(1_000_000, 0))])

    line = cli._status_bar_line(cfg, {"settings": {}}, False, 120)

    assert usage.format_dollars(1.25) in line


def test_status_bar_shows_tokens_when_no_call_was_priced(tmp_path, monkeypatch):
    cfg = replace(_cfg(tmp_path), model="qwen3:32b", provider_id="ollama")
    cfg.session_file.parent.mkdir(parents=True, exist_ok=True)
    _run(cfg, monkeypatch, [_reply(text="hi", use=_usage(40_000, 2_000))])

    line = cli._status_bar_line(cfg, {"settings": {}}, False, 120)

    assert "$" not in line
    assert usage.format_tokens(42_000) in line


def test_status_line_gives_way_with_the_cost_after_the_count():
    bar = dict(clock="23:59", provider="cpa", model="claude-fable-5-1", context_tokens=121000, phase="",
               output_tokens=9642, throbber="*", agent_id="defaultagent", session_short="b61643c8",
               cache_pct=100, cost="$1.23")

    assert "$1.23" in screen.status_line(100, **bar)
    narrow = screen.status_line(40, **bar)
    assert "$1.23" not in narrow
    assert "b61643c8" in narrow
    assert "9,600" not in narrow


def test_status_text_before_any_call_is_empty():
    assert usage.status_text(usage.UsageTotals()) is None


def test_wipe_starts_the_totals_again(tmp_path, monkeypatch, capsys):
    cfg = _priced_cfg(tmp_path)
    _run(cfg, monkeypatch, [_reply(text="hi", use=_usage(100, 10))])

    cli._cmd_wipe("", {"messages": []}, cfg)

    assert usage.totals(Path(cfg.session_file)).calls == 0
