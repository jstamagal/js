import pytest

from js import runtime
from js.text_bytes import byte_size, cap_text
from js.toolkit.core import ToolResult


@pytest.mark.parametrize("cap", [1, 10, 64, 129])
@pytest.mark.parametrize("text", ["é" * 200, "😀" * 200], ids=["two-byte", "four-byte"])
def test_hard_result_caps_include_marker_bytes(cap, text):
    result = runtime._cap_result(text, cap, inline_cap=0)
    assert len(result.encode("utf-8")) <= cap
    assert result != text
    assert "�" not in result
    assert "truncated" in result or result.endswith("~")
    assert runtime._cap_result("é", 2, inline_cap=0) == "é"


def test_spill_counts_utf8_and_budgets_notice_and_preview(tmp_path):
    text = "é😀" * 300
    result = runtime.spill_oversized_result(text, 1000, spill_dir=tmp_path)
    assert "result was 1800 bytes" in result
    assert len(result.encode("utf-8")) <= 1000
    assert "�" not in result
    assert next(tmp_path.glob("result-*.txt")).read_bytes() == text.encode("utf-8")


def test_spill_threshold_is_bytes_even_when_notice_cannot_fit(tmp_path):
    result = runtime.spill_oversized_result("é" * 100, 150, spill_dir=tmp_path)
    assert "result was 200 bytes" in result
    assert result.startswith("[result was")
    assert next(tmp_path.glob("result-*.txt")).read_bytes() == ("é" * 100).encode()


def test_batch_caps_multibyte_and_structured_text():
    results = ["😀" * 100, ToolResult.text("é" * 100), "ok"]
    capped = runtime._cap_batch_results(results, 180)
    assert sum(byte_size(r.dehydrated() if isinstance(r, ToolResult) else r) for r in capped) <= 180
    assert capped[-1] == "ok"


def test_cap_text_zero_means_no_share_not_unlimited():
    assert cap_text("overflow", 0, "[truncated]") == ""


def test_removed_media_discloses_truncation_even_with_short_placeholder():
    result = ToolResult([{"type": "image", "data": "x" * 5000, "mimeType": "image/png"}])
    capped = runtime._cap_result(result, 1000)
    assert "truncated" in capped.dehydrated()
    assert len(capped.dehydrated().encode()) <= 1000