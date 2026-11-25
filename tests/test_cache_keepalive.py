"""The idle cache keepalive: what it resends, when, and when it stops."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import ai
import ai.types.usage
import pytest
from test_turn_call import _done, _Turn

from js import cache_keepalive, compaction, messages as msgs, model_client, settings, status_bar
from js.cache_keepalive import CachedRequest, CacheKeepalive
from js.model_client import ModelStreamResult
from js.toolkit import ToolContext

TTL = 300
DELAY = 270  # 90% of TTL


def _request(*, model="claude-opus-5-5", provider_id="cliproxyapi", thinking_budget=None,
             settings_=None, sent_at=1000.0) -> CachedRequest:
    return CachedRequest(
        model=model, provider_id=provider_id, base_url="http://vader:8999/v1", api_key=None, effort="high",
        thinking_budget=thinking_budget, headers=None, extra=None, sampling=None, cache_key=None,
        settings=settings_, stream_limits=None, messages=(ai.user_message("hi"),), tools=None, sent_at=sent_at,
    )


def _reply(*, prompt=100_000, read=99_000) -> ModelStreamResult:
    return ModelStreamResult(
        text="", tool_calls=[], reasoning="",
        usage=ai.types.usage.Usage(input_tokens=prompt, output_tokens=1, cache_read_tokens=read),
        finish_reason="incomplete:max_output_tokens", assistant_message=ai.assistant_message(""),
    )


class _Idle:
    """A keepalive over a fake clock, scripted refresh replies and live settings."""

    def __init__(self, replies=None, *, request=None, **live):
        self.now = 1000.0
        self.busy = False
        self.route = ("claude-opus-5-5", "cliproxyapi")
        self.sent: list[CachedRequest] = []
        self.said: list[str] = []
        self.compactions = 0
        self.replies = list(replies or [])
        self.context = ToolContext()
        self.context.cached_request = request or _request(sent_at=self.now)
        self.context.last_prompt_tokens = 100_000
        self.live = {"CACHE_KEEPALIVE": True, "COMPACTION_CACHE_TTL": TTL, "CACHE_KEEPALIVE_IDLE_MAX": 3600,
                     "CACHE_KEEPALIVE_COMPACTION": True, "CACHE_KEEPALIVE_COMPACTION_TOKENS_MIN": 50_000, **live}

        async def send(request):
            self.sent.append(request)
            reply = self.replies.pop(0) if self.replies else _reply()
            if isinstance(reply, BaseException):
                raise reply
            return reply

        def compact():
            self.compactions += 1

        self.keepalive = CacheKeepalive(
            context=self.context, live=lambda: self.live, busy=lambda: self.busy, route=lambda: self.route,
            compact=compact, say=lambda said: self.said.append(str(said)), clock=lambda: self.now, send=send,
        )

    def at(self, seconds: float) -> None:
        """Step the keepalive with the clock ``seconds`` after the request went out."""
        self.now = 1000.0 + seconds
        asyncio.run(self.keepalive.step())


@pytest.fixture(autouse=True)
def quiet_network(monkeypatch):
    monkeypatch.setattr(cache_keepalive.stream_transport, "say", lambda level, line: None)


def test_the_defaults_refresh_for_an_hour_and_then_compact():
    assert settings.default_value("CACHE_KEEPALIVE") is True
    assert settings.default_value("CACHE_KEEPALIVE_IDLE_MAX") == 3600
    assert settings.default_value("CACHE_KEEPALIVE_COMPACTION") is True


@pytest.mark.parametrize(("ttl", "delay"), [(300, 270), (3600, 3240), (60, 50), (10, None), (0, None)])
def test_a_refresh_is_due_at_ninety_percent_and_ten_seconds_before_the_end(ttl, delay):
    assert cache_keepalive.refresh_delay(ttl) == delay


def test_claude_routes_are_refreshed_and_others_are_not():
    assert cache_keepalive.not_replayable(_request()) is None
    assert cache_keepalive.not_replayable(_request(model="claude-sonnet-5-5", provider_id="anthropic")) is None
    assert cache_keepalive.not_replayable(_request(model="gpt-5.5", provider_id="cliproxyapi")) is not None
    assert cache_keepalive.not_replayable(_request(model="gpt-5.5", provider_id="openai")) is not None


def test_a_request_whose_thinking_a_one_token_cap_would_change_is_not_refreshed():
    assert cache_keepalive.not_replayable(_request(thinking_budget=8000)) is not None


def test_the_anthropic_wire_with_caching_off_is_not_refreshed():
    request = _request(model="claude-opus-5-5", provider_id="anthropic", settings_={"ANTHROPIC_PROMPT_CACHE": False})
    assert cache_keepalive.not_replayable(request) is not None


def test_nothing_goes_out_before_the_refresh_is_due():
    idle = _Idle()
    idle.at(DELAY - 1)
    assert idle.sent == []
    assert idle.keepalive.next_at == 1000.0 + DELAY


def test_a_due_refresh_resends_the_request_and_renews_the_cache_clock():
    idle = _Idle()
    idle.at(DELAY)
    assert idle.sent == [idle.context.cached_request]
    assert idle.context.last_request_at == 1000.0 + DELAY
    assert idle.keepalive.next_at == 1000.0 + 2 * DELAY
    idle.at(2 * DELAY - 1)
    assert len(idle.sent) == 1
    idle.at(2 * DELAY)
    assert len(idle.sent) == 2
    assert idle.said == []


def test_nothing_goes_out_while_a_turn_runs():
    idle = _Idle()
    idle.busy = True
    idle.at(DELAY)
    assert idle.sent == []
    assert idle.keepalive.next_at is None


def test_nothing_goes_out_with_the_setting_off():
    idle = _Idle(CACHE_KEEPALIVE=False)
    idle.at(DELAY)
    assert idle.sent == []


def test_a_gpt_route_is_never_refreshed():
    idle = _Idle(request=_request(model="gpt-5.5", sent_at=1000.0))
    idle.at(DELAY)
    assert idle.sent == []
    assert idle.keepalive.next_at is None


def test_the_idle_limit_ends_the_refreshing_and_compacts_a_large_context():
    idle = _Idle(CACHE_KEEPALIVE_IDLE_MAX=600)
    idle.at(DELAY)
    idle.at(2 * DELAY)
    idle.at(3 * DELAY)
    assert len(idle.sent) == 2
    assert idle.compactions == 1
    assert len(idle.said) == 1
    assert idle.keepalive.next_at is None
    idle.at(4 * DELAY)
    assert len(idle.sent) == 2
    assert idle.compactions == 1


def test_a_small_context_is_left_whole_at_the_idle_limit():
    idle = _Idle(CACHE_KEEPALIVE_IDLE_MAX=200)
    idle.context.last_prompt_tokens = 10_000
    idle.at(DELAY)
    assert idle.sent == []
    assert idle.compactions == 0


def test_compaction_off_leaves_the_history_whole_at_the_idle_limit():
    idle = _Idle(CACHE_KEEPALIVE_IDLE_MAX=200, CACHE_KEEPALIVE_COMPACTION=False)
    idle.at(DELAY)
    assert idle.compactions == 0


def test_a_zero_idle_limit_sends_nothing_and_says_nothing():
    idle = _Idle(CACHE_KEEPALIVE_IDLE_MAX=0)
    idle.at(DELAY)
    assert (idle.sent, idle.said, idle.compactions) == ([], [], 0)


def test_a_refresh_that_misses_the_cache_stops_the_refreshing():
    idle = _Idle([_reply(prompt=100_000, read=0)])
    idle.at(DELAY)
    assert len(idle.sent) == 1
    assert len(idle.said) == 1
    idle.at(2 * DELAY)
    assert len(idle.sent) == 1


def test_a_failed_refresh_stops_the_refreshing():
    idle = _Idle([RuntimeError("502 bad gateway")])
    idle.at(DELAY)
    assert "502 bad gateway" in idle.said[0]
    idle.at(2 * DELAY)
    assert len(idle.sent) == 1


def test_a_cache_that_ran_out_while_the_machine_slept_is_not_refreshed():
    idle = _Idle()
    idle.at(TTL + 60)
    assert idle.sent == []
    assert len(idle.said) == 1


def test_a_model_switch_stops_the_refreshing():
    idle = _Idle()
    idle.at(10)
    idle.route = ("gpt-5.5", "cliproxyapi")
    idle.at(DELAY)
    assert idle.sent == []


def test_a_new_turn_starts_a_new_idle_stretch():
    idle = _Idle(CACHE_KEEPALIVE_IDLE_MAX=600)
    idle.at(DELAY)
    idle.at(3 * DELAY)
    assert idle.compactions == 1
    idle.context.cached_request = _request(sent_at=1000.0 + 1000)
    idle.at(1000 + DELAY)
    assert len(idle.sent) == 2
    assert idle.sent[-1] is idle.context.cached_request


def test_a_turn_that_starts_during_a_refresh_owns_the_cache():
    idle = _Idle()
    newer = _request(sent_at=1000.0 + DELAY)
    original_send = idle.keepalive._send

    async def send(request):
        idle.context.cached_request = newer
        return await original_send(request)

    idle.keepalive._send = send
    idle.at(DELAY)
    assert idle.context.last_request_at is None


def test_a_history_rewrite_forgets_the_request():
    context = ToolContext()
    context.cached_request = _request()
    compaction.history_rewritten(context)
    assert context.cached_request is None


def test_a_turn_records_what_it_sent_and_the_refresh_resends_it_with_a_one_token_cap(tmp_path, monkeypatch):
    turn = _Turn(tmp_path, monkeypatch, [_done("hello")], provider_id="cliproxyapi")
    turn.call()
    recorded = turn.context.cached_request
    assert recorded is not None
    assert list(recorded.messages) == list(turn.calls[0]["messages"])
    sent: list[dict] = []

    async def stream(**kwargs):
        sent.append(kwargs)
        return _reply()

    monkeypatch.setattr(model_client, "stream_model_async", stream)
    asyncio.run(cache_keepalive.replay(recorded))
    first = turn.calls[0]
    assert sent[0]["max_output_tokens"] == 1
    assert sent[0]["messages"] == list(first["messages"])
    for key in ("model_id", "provider_id", "reasoning_effort", "tools", "cache_key", "sampling",
                "provider_extra", "provider_headers"):
        assert sent[0][key] == first[key], key


def test_the_status_bar_shows_the_next_refresh():
    line = status_bar.status_line(120, clock="12:00", provider="cpa", model="claude-opus-5-5", context_tokens=100,
                                  phase="", output_tokens=None, throbber="", agent_id=None,
                                  session_short="abc", cache_pct=96, keepalive="12:04")
    assert msgs.STATUS_KEEPALIVE.words(at="12:04") in line


def test_the_status_bar_drops_the_keepalive_first_when_narrow():
    line = status_bar.status_line(40, clock="12:00", provider="cpa", model="claude-opus-5-5", context_tokens=100,
                                  phase="", output_tokens=None, throbber="", agent_id=None,
                                  session_short="abc", cache_pct=96, keepalive="12:04")
    assert msgs.STATUS_KEEPALIVE.words(at="12:04") not in line


def test_charges_go_to_the_session_the_request_was_charged_to(tmp_path):
    from js import usage as usage_mod
    session = tmp_path / "s.jsonl"
    idle = _Idle(request=replace(_request(), meter=usage_mod.Meter((session,))))
    idle.at(DELAY)
    assert usage_mod.totals(session).calls == 1


def test_the_idle_compaction_summarizes_the_main_history(tmp_path, monkeypatch, capsys):
    from js import conversation, runtime

    calls: list[dict] = []

    async def compact_now(cfg, system, messages, **kwargs):
        calls.append({"system": system, "messages": messages, **kwargs})
        return msgs.COMPACTED.said(keep_from=2, total=4, model="m")

    monkeypatch.setattr(compaction, "compact_now", compact_now)
    cfg = SimpleNamespace(agent_id="a", settings={"COMPACTION_LOG_DIR": str(tmp_path)})
    history = [{"role": "user", "content": "hi"}]
    state = SimpleNamespace(config_for_turn=lambda _cfg: cfg, system="SYSTEM", messages=history, events=None)
    context = runtime.T.STOCK_CONTEXT
    monkeypatch.setattr(context, "last_prompt_tokens", 80_000)
    asyncio.run(conversation.idle_compact_async(cfg, state))
    assert calls[0]["messages"] is history
    assert calls[0]["context"] is context
    assert calls[0]["trigger"]["phase"] == "idle"
    assert context.last_prompt_tokens == 0
    assert context.turn_status.compacting is False
