from __future__ import annotations

from js import runtime


def _history(n_tools: int, body_chars: int = 5_000) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "sys"}]
    for i in range(n_tools):
        msgs.append({"role": "assistant", "content": f"thinking {i}"})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "read",
                     "content": "x" * body_chars})
    msgs.append({"role": "user", "content": "now what"})
    return msgs


def test_microcompact_clears_old_tool_bodies_and_keeps_recent_ones():
    msgs = _history(30)
    cleared, reclaimed = runtime.microcompact(msgs, keep_recent=20)

    assert cleared == 10
    assert reclaimed > 40_000
    tools = [m for m in msgs if m["role"] == "tool"]
    assert all(t["content"] == runtime.MICROCOMPACT_CLEARED_MESSAGE for t in tools[:10])
    assert all(t["content"] != runtime.MICROCOMPACT_CLEARED_MESSAGE for t in tools[10:])
    # Assistant reasoning about those results is untouched.
    assert all(m["content"].startswith("thinking") for m in msgs if m["role"] == "assistant")


def test_microcompact_leaves_small_results_alone():
    msgs = _history(30, body_chars=10)
    cleared, reclaimed = runtime.microcompact(msgs, keep_recent=0, min_chars=400)
    assert (cleared, reclaimed) == (0, 0)


def test_microcompact_is_idempotent():
    msgs = _history(30)
    first = runtime.microcompact(msgs, keep_recent=0)
    second = runtime.microcompact(msgs, keep_recent=0)
    assert first[0] == 30
    assert second == (0, 0)


def test_microcompact_preserves_tool_call_ids_so_the_history_stays_valid():
    # A tool message orphaned from its call id breaks the next request.
    msgs = _history(5)
    runtime.microcompact(msgs, keep_recent=0)
    tools = [m for m in msgs if m["role"] == "tool"]
    assert [t["tool_call_id"] for t in tools] == [f"c{i}" for i in range(5)]
    assert all(t["name"] == "read" for t in tools)


def test_microcompact_keep_recent_larger_than_history_clears_nothing():
    msgs = _history(3)
    assert runtime.microcompact(msgs, keep_recent=20) == (0, 0)
