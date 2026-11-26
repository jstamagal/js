"""One conversation gets one cache key, so an OpenAI-compatible endpoint can route
a turn to the machine already holding that conversation's prefix."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from test_runtime_cluster_fixes import _cfg, _Recorder

from js import runtime


def _cache_key_for(tmp_path, monkeypatch, cfg):
    seen: dict = {}

    async def capture(**kwargs):
        seen["cache_key"] = kwargs.get("cache_key")
        raise asyncio.CancelledError

    monkeypatch.setattr(runtime.model_client, "stream_model_async", capture)

    async def drive():
        try:
            await runtime.run_turn_async(
                cfg, "SYS", [{"role": "user", "content": "hi"}],
                runtime.Telemetry(debug_log=None),
                suppress_output=True, event_hooks=_Recorder(),
            )
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    return seen.get("cache_key")


def test_a_saved_session_sends_a_cache_key(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    assert _cache_key_for(tmp_path, monkeypatch, cfg)


def test_the_key_fits_openai_length_cap_for_a_derived_session(tmp_path, monkeypatch):
    # A derived session's name is 64 hex digits; OpenAI takes 64 characters.
    cfg = _cfg(tmp_path)
    cfg = replace(cfg, session_file=tmp_path / "derived" / f"{'f' * 64}.jsonl")

    assert len(_cache_key_for(tmp_path, monkeypatch, cfg)) <= 64


def test_same_named_sessions_in_two_directories_do_not_share_a_key(tmp_path, monkeypatch):
    one = replace(_cfg(tmp_path), session_file=tmp_path / "one" / "work.jsonl")
    two = replace(_cfg(tmp_path), session_file=tmp_path / "two" / "work.jsonl")

    assert _cache_key_for(tmp_path, monkeypatch, one) != _cache_key_for(tmp_path, monkeypatch, two)


def test_the_key_is_stable_across_turns_of_one_session(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    first = _cache_key_for(tmp_path, monkeypatch, cfg)
    second = _cache_key_for(tmp_path, monkeypatch, cfg)

    assert first == second


def test_two_sessions_do_not_share_a_key(tmp_path, monkeypatch):
    # A shared key would put two conversations in contention for one prefix.
    one = _cfg(tmp_path, agent="a")
    two = _cfg(tmp_path, agent="b")

    assert _cache_key_for(tmp_path, monkeypatch, one) != _cache_key_for(tmp_path, monkeypatch, two)
