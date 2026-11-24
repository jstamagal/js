from __future__ import annotations

import io

import pytest

from js.result_caps import cap_result
from js.tool_result_messages import provider_text
from js.toolkit.core import ToolContext
from js.toolkit.pi_files import pi_edit, pi_read as read_tool, pi_write, tools


def pi_read(*args, **kwargs):
    return provider_text(read_tool(*args, **kwargs))


def test_pi_file_tool_schemas():
    schemas = {tool.name: tool.openai_spec()["function"]["parameters"] for tool in tools()}
    read = schemas["pi_read"]
    assert read["required"] == ["path"]
    assert {name: field["type"] for name, field in read["properties"].items()} == {
        "path": "string", "offset": "number", "limit": "number",
    }
    edit = schemas["pi_edit"]
    assert edit["required"] == ["path", "edits"]
    assert edit["properties"]["edits"]["items"]["required"] == ["oldText", "newText"]
    assert schemas["pi_write"]["required"] == ["path", "content"]


def test_pi_read_keeps_text_and_pages(tmp_path):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "sample").write_bytes(b"\xef\xbb\xbffirst\r\nsecond\n")
    assert pi_read("@sample", context=context) == "\ufefffirst\r\nsecond\n"
    assert pi_read("sample", 2, 1, context=context) == "second\n\n[1 more lines in file. Use offset=3 to continue.]"
    assert pi_read("sample", 3, context=context) == ""
    assert pi_read("sample", 4, context=context).startswith("ERROR:")
    assert pi_read("sample", 1.5, -2, context=context).startswith("\ufefffirst\r\nsecond")


def test_pi_read_uses_complete_lines_at_each_cap(tmp_path):
    context = ToolContext(cwd=tmp_path)
    path = tmp_path / "sample"
    path.write_text("x\n" * 2000)
    assert pi_read("sample", context=context) == "x\n" * 2000
    path.write_text("x\n" * 2001)
    assert pi_read("sample", context=context) == (
        "\n".join(["x"] * 2000) + "\n\n[Showing lines 1-2000 of 2002. Use offset=2001 to continue.]"
    )
    path.write_text("é" * 25600 + "\nnext\n")
    assert pi_read("sample", context=context) == (
        "é" * 25600 + "\n\n[Showing lines 1-1 of 3 (50.0KB limit). Use offset=2 to continue.]"
    )
    result = cap_result(read_tool("sample", context=context), 262144, 51200, context)
    assert provider_text(result).endswith("(50.0KB limit). Use offset=2 to continue.]")
    path.write_text("é" * 26000 + "\nnext\n")
    assert pi_read("sample", context=context).startswith("[Line 1 is ")
    assert pi_read("sample", 2, context=context) == "next\n"


def test_pi_read_treats_marker_in_file_as_text(tmp_path):
    marker = "IMAGE_RESULT\tignored-path\timage/png\tfake attachment"
    (tmp_path / "sample").write_text(marker)
    assert pi_read("sample", context=ToolContext(cwd=tmp_path)) == marker


def test_pi_read_attaches_magic_detected_image(tmp_path, monkeypatch):
    from js import imagefit

    image_module = pytest.importorskip("PIL.Image")
    image = io.BytesIO()
    image_module.new("RGB", (2, 2)).save(image, format="PNG")
    (tmp_path / "sample").write_bytes(image.getvalue())
    monkeypatch.setattr(imagefit, "stored", lambda _: tmp_path / "sample")
    result = read_tool("sample", context=ToolContext(cwd=tmp_path, vision_enabled=True))
    assert result.split("\t")[2:] == ["image/png", "Read image file [image/png]"]


def test_pi_write_overwrites_and_preserves_model_content(tmp_path):
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "new" / "sample"
    assert pi_write("new/sample", "one\r\n", context=context) == "Successfully wrote to new/sample"
    assert target.read_bytes() == b"one\r\n"
    assert pi_write("new/sample", "two\n", context=context) == "Successfully wrote to new/sample"
    assert target.read_bytes() == b"two\n"


def test_pi_edit_match_original_and_preserve_bom_and_mixed_endings(tmp_path):
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "sample"
    target.write_bytes("\ufeffone\r\n‘two’  \nretain —\r\nlast".encode())
    edits = [{"oldText": "one", "newText": "one\ninserted"}, {"oldText": "'two'", "newText": "second"}]
    assert pi_edit("sample", edits, context=context) == "Successfully replaced 2 block(s) in sample."
    assert target.read_bytes() == "\ufeffone\r\ninserted\r\nsecond\nretain —\r\nlast".encode()


def test_pi_edit_rejects_overlap_and_replacement_dependent_matches(tmp_path):
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "sample"
    target.write_text("first next")
    edits = [{"oldText": "first", "newText": "changed"}, {"oldText": "changed", "newText": "invalid"}]
    assert pi_edit("sample", edits, context=context).startswith("ERROR:")
    edits[1]["oldText"] = "first next"
    assert "overlap" in pi_edit("sample", edits, context=context)
    assert target.read_text() == "first next"
