"""The summary request that reuses the prompt cache: the conversation's last
request, extended to the current history, with the summary instructions last."""

from dataclasses import replace

import ai
import pytest
from test_compaction_breaker_and_cache import _config, _turn
from test_lazy_tool_discovery import _result

from js import compaction, compaction_summary, context_window, model_client, runtime
from js.toolkit import ToolContext

BODY = "HEAD" + "y" * 10_000 + "TAIL"
CHECKPOINT = "SUMMARY\n" + "\n".join(f"## {h}\n-" for h in compaction_summary._COMPACTION_HEADINGS)


@pytest.fixture(autouse=True)
def offline_metadata(monkeypatch):
    monkeypatch.setattr(context_window, "resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)


def _history() -> list[dict]:
    return [
        {"role": "user", "content": "read a.py " + "early " * 400},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read", "content": BODY},
        {"role": "assistant", "content": "read it"},
        {"role": "user", "content": "now fix it"},
    ]


class _Provider:
    """Answers turns with "done" and summaries with "SUMMARY", keeping every request."""

    def __init__(self, *, overflow_replay: bool = False, refuse_replay: str = "", replay_reply: str = CHECKPOINT):
        self.turns: list[dict] = []
        self.summaries: list[dict] = []
        self.refuse_replay = "context_length_exceeded" if overflow_replay else refuse_replay
        self.replay_reply = replay_reply

    def is_summary(self, kwargs) -> bool:
        last = kwargs["messages"][-1]
        text = "".join(getattr(p, "text", "") for p in last.parts)
        return "context compaction" in text or text.startswith("Summarize this js session")

    async def __call__(self, **kwargs):
        kwargs = {**kwargs, "messages": list(kwargs["messages"])}  # the turn appends to its list
        if not self.is_summary(kwargs):
            self.turns.append(kwargs)
            return _result(text="done")
        self.summaries.append(kwargs)
        if len(kwargs["messages"]) == 1:
            return _result(text=CHECKPOINT)
        if self.refuse_replay:
            raise ai.ProviderAPIError(self.refuse_replay, provider="openai")
        return _result(text=self.replay_reply)


def _dump(messages) -> list[dict]:
    """``messages`` as they go over the wire: without the SDK's own message and part ids."""
    out = []
    for message in messages:
        record = message.model_dump(mode="json", exclude={"id"})
        record["parts"] = [{k: v for k, v in part.items() if k != "id"} for part in record["parts"]]
        out.append(record)
    return out


def _compact(cfg, messages, context):
    return compaction.compact_now_sync(cfg, "system", messages, forced=True, context=context)


def _setup(monkeypatch, tmp_path, provider, **settings):
    cfg = _config(tmp_path, {"COMPACTION_KEEP_RECENT_TOKENS": 10, "COMPACTION_MIN_SAVINGS_TOKENS": 1, **settings})
    context = ToolContext(cwd=tmp_path)
    monkeypatch.setattr(model_client, "_stream_async", provider)
    messages = _history()
    _turn(cfg, messages, context)
    assert messages[-1]["content"] == "done"
    return cfg, context, messages


def test_summary_extends_the_last_request_with_the_instructions(monkeypatch, tmp_path):
    provider = _Provider()
    cfg, context, messages = _setup(monkeypatch, tmp_path, provider)
    (turn,) = provider.turns
    assert compaction.compacted(_compact(cfg, messages, context))

    (summary,) = provider.summaries
    sent = _dump(summary["messages"])
    # The turn's request is a prefix of the summary request, so the provider
    # reads it from the cache; the turn's reply follows, then the instructions.
    assert sent[:len(turn["messages"])] == _dump(turn["messages"])
    assert len(sent) == len(turn["messages"]) + 2
    assert sent[-2]["role"] == "assistant" and "done" in str(sent[-2])
    assert sent[-1]["role"] == "user"
    assert summary["tools"] == turn["tools"]
    assert BODY in str(sent)  # whole, not clipped
    assert any(m.get("content", "").startswith("<compaction-summary>\nSUMMARY") for m in messages)


def test_rewritten_history_sends_the_transcript(monkeypatch, tmp_path):
    provider = _Provider()
    cfg, context, messages = _setup(monkeypatch, tmp_path, provider)
    messages[2] = {**messages[2], "content": "[old tool result cleared]"}
    assert compaction.compacted(_compact(cfg, messages, context))
    (summary,) = provider.summaries
    assert len(summary["messages"]) == 1 and summary["tools"] is None


def test_reuse_off_sends_the_transcript(monkeypatch, tmp_path):
    provider = _Provider()
    cfg, context, messages = _setup(monkeypatch, tmp_path, provider, COMPACTION_REUSE_CACHE=False)
    assert compaction.compacted(_compact(cfg, messages, context))
    (summary,) = provider.summaries
    assert len(summary["messages"]) == 1


def test_another_summary_model_sends_the_transcript(monkeypatch, tmp_path):
    provider = _Provider()
    cfg, context, messages = _setup(monkeypatch, tmp_path, provider)
    context.cached_request = replace(context.cached_request, model="some-other-model")
    assert compaction.compacted(_compact(cfg, messages, context))
    (summary,) = provider.summaries
    assert len(summary["messages"]) == 1


def test_replay_too_long_falls_back_to_the_transcript(monkeypatch, tmp_path):
    provider = _Provider(overflow_replay=True)
    cfg, context, messages = _setup(monkeypatch, tmp_path, provider)
    assert compaction.compacted(_compact(cfg, messages, context))
    replay, transcript = provider.summaries
    assert len(replay["messages"]) > 1
    assert len(transcript["messages"]) == 1


def test_a_replay_the_endpoint_refuses_for_its_shape_falls_back_to_the_transcript(monkeypatch, tmp_path):
    provider = _Provider(refuse_replay="Conversation roles must alternate user/assistant/user/assistant")
    cfg, context, messages = _setup(monkeypatch, tmp_path, provider)
    assert compaction.compacted(_compact(cfg, messages, context))
    replay, transcript = provider.summaries
    assert len(replay["messages"]) > 1
    assert len(transcript["messages"]) == 1


def test_a_replay_answered_as_the_agent_is_not_the_summary(monkeypatch, tmp_path):
    provider = _Provider(replay_reply='{"status": "working", "next": "run the tests"}')
    cfg, context, messages = _setup(monkeypatch, tmp_path, provider)
    assert compaction.compacted(_compact(cfg, messages, context))
    replay, transcript = provider.summaries
    assert len(replay["messages"]) > 1 and len(transcript["messages"]) == 1
    assert not any('"status": "working"' in str(m.get("content", "")) for m in messages)
    assert any(str(m.get("content", "")).startswith("<compaction-summary>\nSUMMARY") for m in messages)


def test_compaction_forgets_the_request_it_rewrote(monkeypatch, tmp_path):
    provider = _Provider()
    cfg, context, messages = _setup(monkeypatch, tmp_path, provider)
    assert compaction.compacted(_compact(cfg, messages, context))
    assert context.cached_request is None


def test_a_changed_endpoint_key_or_header_sends_the_transcript(monkeypatch, tmp_path):
    for change in ({"base_url": "https://elsewhere.invalid/v1"}, {"api_key": "another-key"},
                   {"headers": {"X-Tenant": "another"}}):
        provider = _Provider()
        cfg, context, messages = _setup(monkeypatch, tmp_path, provider)
        # The last request went to another endpoint, or under another key or header, than one sent now would.
        context.cached_request = replace(context.cached_request, **change)
        assert compaction.compacted(_compact(cfg, messages, context))
        (summary,) = provider.summaries
        assert len(summary["messages"]) == 1


def test_the_summary_output_cap_stays_under_the_models_output_limit(monkeypatch, tmp_path):
    provider = _Provider()
    cfg, context, messages = _setup(monkeypatch, tmp_path, provider)   # the model's limit here is 4096
    assert compaction.compacted(_compact(cfg, messages, context))
    (replay,) = provider.summaries
    assert replay["params"].output.max_tokens == 4096

    provider = _Provider()
    cfg, context, messages = _setup(monkeypatch, tmp_path, provider, COMPACTION_REUSE_CACHE=False)
    assert compaction.compacted(_compact(cfg, messages, context))
    (transcript,) = provider.summaries
    assert transcript["params"].output.max_tokens == 4096


def test_extra_headers_are_replayed_until_they_change(monkeypatch, tmp_path):
    def after_a_turn(provider):
        cfg = replace(_config(tmp_path, {"COMPACTION_KEEP_RECENT_TOKENS": 10, "COMPACTION_MIN_SAVINGS_TOKENS": 1}),
                      extra_headers={"X-Tenant": "mine"})
        context = ToolContext(cwd=tmp_path)
        monkeypatch.setattr(model_client, "_stream_async", provider)
        messages = _history()
        _turn(cfg, messages, context)
        assert context.cached_request.headers["X-Tenant"] == "mine"
        return cfg, context, messages

    provider = _Provider()
    cfg, context, messages = after_a_turn(provider)
    assert compaction.compacted(_compact(cfg, messages, context))
    (summary,) = provider.summaries
    assert len(summary["messages"]) > 1

    provider = _Provider()
    cfg, context, messages = after_a_turn(provider)
    assert compaction.compacted(_compact(replace(cfg, extra_headers={"X-Tenant": "theirs"}), messages, context))
    (summary,) = provider.summaries
    assert len(summary["messages"]) == 1
