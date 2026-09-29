"""The turn's context budget: the conversation the model is sent, and how a
turn brings it under the window before and after the provider refuses it."""

from __future__ import annotations

import asyncio
import io
import time
from dataclasses import replace
from types import SimpleNamespace

import ai
import pytest

from js import compaction, context_budget, model_client, runtime
from js import messages as msgs
from js.toolkit import ToolContext
from js.toolkit.core import TurnStatus
from js.toolkit.registry import build_default_registry
from js.turn_budget import TurnBudget, TurnConvo
from test_lazy_tool_discovery import _cfg, _result


@pytest.fixture(autouse=True)
def offline_metadata(monkeypatch):
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)


def _overflow():
    return ai.ProviderAPIError(
        "request (97720 tokens) exceeds the available context size (89600 tokens)",
        provider="openai", error_type="exceed_context_size_error",
    )


def _tool_history(n: int) -> list[dict]:
    messages: list[dict] = []
    for index in range(n):
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{index}", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{index}", "name": "read", "content": "x" * 1000},
        ])
    messages.append({"role": "user", "content": "continue"})
    return messages


def test_overflow_cleared_resends_the_rebuilt_conversation_from_the_top(monkeypatch, tmp_path):
    cfg = replace(_cfg(tmp_path), provider_id="openai", provider_api_key="offline",
                  provider_base_url="http://127.0.0.1:1/v1")
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _overflow()
        return _result(text="ok")

    monkeypatch.setattr(model_client, "stream_model_async", stream)
    messages = _tool_history(22)
    asyncio.run(runtime.run_turn_async(
        cfg, "SYSTEM", messages, runtime.Telemetry(None, trace_sink=io.StringIO()),
        tool_registry=build_default_registry().select([]),
        tool_context=ToolContext(cwd=tmp_path), suppress_output=True,
    ))
    assert len(calls) == 2
    assert compaction.MICROCOMPACT_CLEARED_MESSAGE not in str(calls[0]["messages"])
    assert compaction.MICROCOMPACT_CLEARED_MESSAGE in str(calls[1]["messages"])
    assert calls[1]["trace_request_from"] == 0
    assert calls[1]["trace_request_schemas"] is True
    assert messages[-1]["content"] == "ok"


# --------------------------------------------------------------------------
# TurnConvo
# --------------------------------------------------------------------------

def _convo(messages):
    return TurnConvo("SYSTEM", messages, provider_id=None, model="offline-test")


def test_the_convo_is_the_sdk_form_of_the_history():
    convo = _convo([{"role": "user", "content": "hi"}])
    assert [m.role for m in convo.ai] == ["system", "user"]
    assert (convo.sent, convo.schemas) == (0, True)


def test_a_rebuild_replaces_the_list_and_restarts_the_trace():
    messages = [{"role": "user", "content": "hi"}]
    convo = _convo(messages)
    before = convo.ai
    convo.traced()
    assert (convo.sent, convo.schemas) == (2, False)
    messages.append({"role": "assistant", "content": "hello"})
    convo.rebuild()
    assert [m.role for m in before] == ["system", "user"]
    assert [m.role for m in convo.ai] == ["system", "user", "assistant"]
    assert (convo.sent, convo.schemas) == (0, True)


def test_added_records_extend_the_convo_without_a_system_message():
    messages = [{"role": "user", "content": "hi"}]
    convo = _convo(messages)
    convo.traced()
    nudge = {"role": "user", "content": "go on"}
    messages.append(nudge)
    convo.add([nudge])
    assert [m.role for m in convo.ai] == ["system", "user", "user"]
    assert (convo.sent, convo.schemas) == (2, False)


# --------------------------------------------------------------------------
# TurnBudget
# --------------------------------------------------------------------------

class _Setup:
    """A TurnBudget over ``messages`` with a known window, and what it did."""

    def __init__(self, tmp_path, messages, *, window=8000, warm=False, compact=None):
        settings = {"flight_log_dir": str(tmp_path / "flights"), **(compact or {})}
        self.cfg = _cfg(tmp_path, {"compact": settings})
        self.messages = messages
        self.convo = TurnConvo("SYSTEM", messages, provider_id=None, model="offline-test")
        self.tokens = context_budget.TokenState(chars_per_token=4.0)
        self.context = ToolContext(cwd=tmp_path)
        if warm:
            self.context.last_request_at = time.time()
        self.context.cache_read_baseline = 1234
        self.events: list[tuple[str, dict]] = []
        self.budget = TurnBudget(
            self.cfg, self.convo, self.tokens, self.context,
            telemetry=SimpleNamespace(event=lambda kind, **fields: self.events.append((kind, fields))),
            turn_status=TurnStatus(), emit=lambda *a, **k: None,
            resolve_window=lambda: window, max_out=4096,
        )

    def kinds(self):
        return [kind for kind, _ in self.events]

    def fit(self, **kwargs):
        return asyncio.run(self.budget.fit(specs=[], **kwargs))


def _summaries(monkeypatch, *, fail=False):
    """Replace the summary model: each call replaces the summarized span with
    one short summary message."""
    calls = []

    async def compact_now(cfg, system, messages, **kwargs):
        calls.append(kwargs)
        if fail:
            raise RuntimeError("summary model down")
        start = kwargs["preserve_from"]
        if start is None:
            start = compaction.tail_start(messages, kwargs["tail_tokens"], 4.0)
        messages[:start] = [{"role": "user", "content": "<compaction-summary>\nshort\n</compaction-summary>"}]
        return msgs.COMPACTED.said(keep_from=start, total=len(messages), model="fixture")

    monkeypatch.setattr(compaction, "compact_now", compact_now)
    return calls


def _prefix_then_turn(n_prefix: int, n_turn: int) -> list[dict]:
    """Earlier turns with ``n_prefix`` tool results, then the current user
    message and ``n_turn`` tool results of this turn."""
    earlier = [{"role": "user", "content": "earlier task"}, *_tool_history(n_prefix)[:-1]]
    current = _tool_history(n_turn)
    current = [current[-1], *current[:-1]]
    for index, message in enumerate(current):
        if message.get("role") == "assistant":
            message["tool_calls"][0]["id"] = f"t{index}"
        elif message.get("role") == "tool":
            message["tool_call_id"] = f"t{index - 1}"
    return earlier + current


def test_a_request_under_budget_is_left_alone(tmp_path, monkeypatch):
    calls = _summaries(monkeypatch)
    setup = _Setup(tmp_path, _tool_history(2), window=1_000_000)
    before = setup.convo.ai
    assert setup.fit(phase="preflight") is False
    assert setup.kinds() == ["context_budget"]
    assert setup.context.context_tokens > 0
    assert setup.convo.ai is before
    assert calls == []


def test_auto_off_skips_the_check_unless_forced(tmp_path, monkeypatch):
    _summaries(monkeypatch)
    setup = _Setup(tmp_path, _tool_history(22), compact={"auto": False})
    assert setup.fit(phase="preflight") is False
    assert setup.events == []


def test_a_cold_cache_clears_old_tool_results_first(tmp_path, monkeypatch):
    calls = _summaries(monkeypatch)
    setup = _Setup(tmp_path, _prefix_then_turn(0, 22))
    before = setup.convo.ai
    assert setup.fit(phase="preflight") is True
    assert "context_results_cleared" in setup.kinds()
    assert calls == []
    assert setup.convo.ai is not before
    assert compaction.MICROCOMPACT_CLEARED_MESSAGE in str(setup.convo.ai)
    assert setup.context.compacted_during_turn is True
    assert setup.context.cache_read_baseline is None


def test_a_warm_cache_summarizes_earlier_turns_before_clearing(tmp_path, monkeypatch):
    calls = _summaries(monkeypatch)
    messages = _prefix_then_turn(22, 1)
    opening = next(i for i, m in enumerate(messages) if m["content"] == "continue")
    setup = _Setup(tmp_path, messages, warm=True)
    assert setup.fit(phase="preflight") is True
    assert setup.kinds()[:2] == ["context_budget", "context_clearing_deferred"]
    assert "context_results_cleared" not in setup.kinds()
    assert [call["preserve_from"] for call in calls] == [opening]
    assert "<compaction-summary>" in str(setup.convo.ai)


def test_a_turn_over_budget_on_its_own_summarizes_itself_keeping_a_tail(tmp_path, monkeypatch):
    calls = _summaries(monkeypatch)
    # The opening message alone fills the window, so clearing cannot help.
    setup = _Setup(tmp_path, [{"role": "user", "content": "go " * 40000}, *_tool_history(6)[:-1]], warm=True)
    assert setup.fit(phase="midturn") is True
    kinds = setup.kinds()
    assert kinds.index("context_clearing_deferred") < kinds.index("context_results_cleared")
    assert len(calls) == 1
    assert calls[0]["preserve_from"] is None
    assert calls[0]["tail_tokens"] == compaction.get_int(setup.cfg, "tail_tokens")


@pytest.mark.parametrize("overflow_round", [1, 2, 3])
def test_a_forced_fit_keeps_half_as_much_tail_each_round(tmp_path, monkeypatch, overflow_round):
    calls = _summaries(monkeypatch)
    setup = _Setup(tmp_path, [{"role": "user", "content": "go " * 40000}, *_tool_history(4)[:-1]],
                   window=1_000_000)
    history_tokens = int(compaction.history_chars(setup.messages) / 4.0)
    setup.fit(phase="overflow_recovery", force=True, overflow_round=overflow_round)
    tail = min(compaction.get_int(setup.cfg, "tail_tokens"), history_tokens)
    assert [call["tail_tokens"] for call in calls] == [tail // 2 ** overflow_round]


def test_a_failed_summary_is_not_retried_in_the_same_check(tmp_path, monkeypatch):
    calls = _summaries(monkeypatch, fail=True)
    setup = _Setup(tmp_path, _prefix_then_turn(22, 1), warm=True)
    setup.fit(phase="preflight")
    assert len(calls) == 1
    assert "context_compaction_failed" in setup.kinds()
    assert setup.context.summary_failures == 1


def test_overflow_recovery_clears_and_rebuilds_the_convo(tmp_path, monkeypatch):
    calls = _summaries(monkeypatch)
    setup = _Setup(tmp_path, _tool_history(22), window=1_000_000)
    setup.convo.traced()
    changed = asyncio.run(setup.budget.recover_overflow(_overflow(), overflow_round=1, tools=[], specs=[]))
    assert changed is True
    assert calls == []
    assert compaction.MICROCOMPACT_CLEARED_MESSAGE in str(setup.convo.ai)
    assert (setup.convo.sent, setup.convo.schemas) == (0, True)
    assert setup.context.compacted_during_turn is True
    assert setup.context.cache_read_baseline is None


def test_overflow_recovery_with_nothing_to_clear_summarizes(tmp_path, monkeypatch):
    calls = _summaries(monkeypatch)
    setup = _Setup(tmp_path, [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "x" * 4000},
                              {"role": "user", "content": "continue"}], window=1_000_000)
    changed = asyncio.run(setup.budget.recover_overflow(_overflow(), overflow_round=1, tools=[], specs=[]))
    assert changed is True
    assert [call["preserve_from"] for call in calls] == [2]


@pytest.mark.parametrize(("resolved", "configured", "expected"), [
    (1000, 0, 1000), (1000, 5000, 5000), (None, 0, None), (None, 700, 700),
])
def test_the_provider_window_is_the_larger_known_window(tmp_path, resolved, configured, expected):
    setup = _Setup(tmp_path, [], window=resolved, compact={"context_window": configured})
    assert setup.budget.provider_window() == expected
