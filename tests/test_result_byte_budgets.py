import base64
import json
import os

import ai
import ai.types.messages
import pytest

from js import model_client, runtime, settings
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


def test_dispatch_spills_by_the_active_context_inline_limit(tmp_path, monkeypatch):
    from js import paths
    from js.toolkit.core import Tool, ToolContext
    from js.toolkit.registry import ToolRegistry

    monkeypatch.setattr(runtime.T.STOCK_CONTEXT, "max_tool_result_inline_bytes", 0)
    payload = "é" * 5000
    registry = ToolRegistry((Tool("probe", "test", lambda: payload, {}),), {})
    context = ToolContext(cwd=tmp_path, max_tool_result_inline_bytes=1000)

    _, delivered = runtime._dispatch("probe", "{}", runtime.Telemetry(None), cap_bytes=0,
                                     registry=registry, tool_context=context)

    assert byte_size(delivered) < byte_size(payload)
    saved = next(paths.tool_results_dir().glob("result-*.txt"))
    assert saved.read_bytes() == payload.encode("utf-8")
    assert str(saved) in delivered


def test_batch_caps_multibyte_and_structured_text():
    results = ["😀" * 100, ToolResult.text("é" * 100), "ok"]
    capped = runtime._cap_batch_results(results, 180)
    assert sum(byte_size(r.dehydrated() if isinstance(r, ToolResult) else r) for r in capped) <= 180
    assert capped[-1] == "ok"


def test_cap_text_zero_means_no_share_not_unlimited():
    assert cap_text("overflow", 0, "[truncated]") == ""


def test_media_over_tool_media_max_is_replaced_by_a_line_naming_the_cap(monkeypatch):
    monkeypatch.setattr(runtime.T.STOCK_CONTEXT, "max_tool_media_bytes", 1000)
    result = ToolResult([{"type": "image", "data": "x" * 5000, "mimeType": "image/png"}])
    capped = runtime._cap_result(result, 100000)
    assert [block["type"] for block in capped.blocks] == ["text"]
    assert "TOOL_MEDIA_MAX" in capped.blocks[0]["text"]


def _screenshot(raw_bytes: int) -> ToolResult:
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(raw_bytes)
    return ToolResult([
        {"type": "text", "text": "screenshot taken"},
        {"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"},
    ])


@pytest.mark.parametrize("raw_bytes, count", [(160_000, 1), (600_000, 1), (160_000, 3)])
def test_screenshots_reach_the_provider_under_the_stock_caps(raw_bytes, count):
    per_result = settings.default_value("TOOL_RESULT_MAX")
    per_batch = settings.default_value("PARALLEL_RESULTS_MAX")
    results = [runtime._cap_result(_screenshot(raw_bytes), per_result) for _ in range(count)]
    for result in runtime._cap_batch_results(results, per_batch):
        messages = model_client.build_tool_result_messages("call-1", "screenshot", result)
        parts = [part for message in messages for part in message.parts]
        assert any(isinstance(part, ai.types.messages.FilePart) for part in parts)

def test_dispatch_caps_media_by_the_context_running_the_call(tmp_path):
    from js.toolkit.core import Tool, ToolContext
    from js.toolkit.registry import ToolRegistry

    def snap(context=None):
        return ToolResult([{"type": "image", "data": "x" * 5000, "mimeType": "image/png"}])

    registry = ToolRegistry((Tool("snap", "test", snap, {}),), {})
    context = ToolContext(cwd=tmp_path, max_tool_media_bytes=1000)
    _, result = runtime._dispatch("snap", "{}", runtime.Telemetry(None), cap_bytes=100000,
                                  registry=registry, tool_context=context)
    assert [block["type"] for block in result.blocks] == ["text"]
    assert "TOOL_MEDIA_MAX" in result.blocks[0]["text"]


def _resource_png(raw_bytes: int) -> ToolResult:
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(raw_bytes)
    return ToolResult([
        {"type": "text", "text": "page captured"},
        {"type": "resource", "resource": {"uri": "screen://1", "mimeType": "image/png",
                                          "blob": base64.b64encode(png).decode()}},
    ])


def test_an_embedded_resource_image_reaches_the_provider_under_the_stock_caps():
    result = runtime._cap_result(_resource_png(160_000), settings.default_value("TOOL_RESULT_MAX"))
    result = runtime._cap_batch_results([result], settings.default_value("PARALLEL_RESULTS_MAX"))[0]
    messages = model_client.build_tool_result_messages("call-1", "screenshot", result)
    parts = [part for message in messages for part in message.parts]
    assert any(isinstance(part, ai.types.messages.FilePart) for part in parts)


def test_the_media_notice_survives_text_clipping(monkeypatch):
    monkeypatch.setattr(runtime.T.STOCK_CONTEXT, "max_tool_media_bytes", 1000)
    result = ToolResult([{"type": "text", "text": "x" * 4000},
                         {"type": "image", "data": "y" * 5000, "mimeType": "image/png"}])
    capped = runtime._cap_result(result, 1000)
    assert "TOOL_MEDIA_MAX" in capped.dehydrated()
    assert "truncated" in capped.dehydrated()


def _sent(result) -> tuple[list[str], bool]:
    """The texts a result becomes on the wire and in the session, and whether
    any media part reaches the provider. The first is every text the provider
    gets for it, joined: what one request carries."""
    messages = model_client.build_tool_result_messages("c1", "t", result)
    texts: list[str] = []
    media = False
    for message in messages:
        for part in message.parts:
            if isinstance(part, ai.types.messages.FilePart) and str(part.media_type).startswith("text/"):
                # The SDK sends a text file part as text.
                texts.append(bytes(part.data).decode("utf-8", "replace"))
            elif isinstance(part, ai.types.messages.FilePart):
                media = True
            elif hasattr(part, "result"):
                texts.append(str(part.result))
            elif hasattr(part, "text"):
                texts.append(part.text)
    texts = ["\n".join(texts)]
    if isinstance(result, ToolResult):
        texts.append(result.dehydrated())
    return texts, media


@pytest.mark.parametrize("name", [b"\xff\xfe-shot.png", "ünï-çødé.png".encode()], ids=["invalid-utf8", "non-ascii"])
@pytest.mark.parametrize("cap", [20, 100000])
def test_an_image_with_an_awkward_file_name_reaches_the_model_and_the_session(tmp_path, name, cap):

    from js.toolkit import fs
    from js.toolkit.core import ToolContext

    raw = os.path.join(os.fsencode(tmp_path), name)
    with open(raw, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
    context = ToolContext(cwd=tmp_path)
    context.vision_enabled = True
    marker = fs.fs_read(os.fsdecode(raw), context=context)

    capped = runtime._cap_result(marker, cap)
    texts, _media = _sent(capped)
    record = model_client.tool_result_record("c1", "read", capped)

    for text in [*texts, json.dumps(record, ensure_ascii=False)]:
        text.encode("utf-8")
