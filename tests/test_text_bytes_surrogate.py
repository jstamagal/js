"""A lone surrogate in tool text (json.loads('"\\ud800"'), a surrogateescape
read) must not crash the byte budgets: text_bytes and the spill path count and
write it as the `?` that errors="replace" gives. ALL-CLAIMS.md:449,586."""
from __future__ import annotations

from js import runtime
from js.text_bytes import byte_prefix, byte_size, cap_text

LONE = "x\udcff"


def test_byte_helpers_take_a_lone_surrogate():
    assert byte_size(LONE) == 2
    assert byte_prefix(LONE * 3, 3) == "x?x"
    capped = cap_text(LONE * 10, 8, "~")
    assert byte_size(capped) <= 8 and capped.endswith("~")


def test_under_budget_text_comes_back_unchanged():
    assert cap_text(LONE, 100, "~") == LONE


def test_cap_result_below_the_spill_size_does_not_raise():
    out = runtime._cap_result(LONE * 10, 8, 4_000)
    assert byte_size(out) <= 8


def test_cap_result_under_every_cap_returns_strictly_encodable_text():
    out = runtime._cap_result(LONE, 100, 4_000)
    assert out == "x?"
    out.encode("utf-8")


def test_cap_result_over_the_spill_size_spills_and_notice_offsets_match(tmp_path):
    text = (LONE + "\n") * 2_000
    out = runtime.spill_oversized_result(text, 1_000, spill_dir=tmp_path)
    files = list(tmp_path.glob("result-*.txt"))
    assert len(files) == 1
    data = files[0].read_bytes()
    assert len(data) == byte_size(text)
    assert b"?" in data and str(files[0]) in out


def test_typed_result_under_budget_reaches_the_model_strictly_encodable():
    from js import model_client
    from js.toolkit.core import ToolResult

    result = ToolResult([
        {"type": "text", "text": LONE},
        {"type": "structured", "value": {"x": "\udcff", "n": [1, "\ud800"]}},
        {"type": "resource", "resource": {"uri": "u", "text": "\udcff"}},
    ], is_error=True)
    out = runtime._cap_result(result, 10_000, 0)
    assert out.is_error
    assert out.blocks[0]["text"] == "x?"
    assert out.blocks[1]["value"] == {"x": "?", "n": [1, "?"]}
    text, _media, _err = model_client._mixed_result_parts(out)
    text.encode("utf-8")


def test_clean_typed_result_is_passed_through_as_is():
    from js.toolkit.core import ToolResult

    result = ToolResult.text("fine")
    assert runtime._cap_result(result, 10_000, 0) is result
