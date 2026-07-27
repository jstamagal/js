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


def test_spill_writes_the_full_result_and_returns_a_pointer(tmp_path):
    big = "y" * 200_000
    out = runtime.spill_oversized_result(big, 50_000, spill_dir=tmp_path)

    assert len(out) < len(big)
    assert "limits.max_tool_result_inline_bytes" in out
    written = list(tmp_path.glob("result-*.txt"))
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == big
    assert str(written[0]) in out


def test_spill_leaves_small_results_alone_and_can_be_disabled(tmp_path):
    small = "z" * 100
    assert runtime.spill_oversized_result(small, 50_000, spill_dir=tmp_path) == small
    big = "z" * 200_000
    assert runtime.spill_oversized_result(big, 0, spill_dir=tmp_path) == big
    assert list(tmp_path.glob("result-*.txt")) == []


def test_post_compact_rehydration_reattaches_recent_files(tmp_path):
    a = tmp_path / "a.py"
    a.write_text("print('a')\n", encoding="utf-8")
    b = tmp_path / "b.py"
    b.write_text("print('b')\n", encoding="utf-8")

    class Ctx:
        read_paths = {a, b}

    msg = runtime._post_compact_rehydration(Ctx())
    assert msg["role"] == "user"
    assert "print('a')" in msg["content"]
    assert "print('b')" in msg["content"]
    assert "post-compaction-files" in msg["content"]


def test_post_compact_rehydration_names_but_skips_huge_files(tmp_path):
    big = tmp_path / "big.log"
    big.write_text("q" * 500_000, encoding="utf-8")

    class Ctx:
        read_paths = {big}

    msg = runtime._post_compact_rehydration(Ctx(), per_file_tokens=1_000)
    assert "too large to re-attach" in msg["content"]
    assert "qqqq" not in msg["content"]


def test_post_compact_rehydration_is_none_without_reads():
    class Ctx:
        read_paths = set()

    assert runtime._post_compact_rehydration(Ctx()) is None
