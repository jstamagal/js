"""Post-compaction file attachment preserves text and reports omitted files."""

from types import SimpleNamespace

import pytest

from js.compaction import _post_compact_rehydration


def rehydrate(*paths, **kwargs):
    context = SimpleNamespace(read_paths=set(paths), resolve_path=lambda path: path)
    message = _post_compact_rehydration(context, **kwargs)
    return message["content"] if message else ""


@pytest.mark.parametrize("name,data", [
    ("shot.png", b"\x89PNG\r\n\x1a\n\x00BINARY_PAYLOAD"),
    ("paper.pdf", b"%PDF-1.7\nBINARY_PAYLOAD"),
    ("blob.dat", b"\x00BINARY_PAYLOAD"),
    ("bad.txt", b"\xffBINARY_PAYLOAD"),
])
def test_nontext_files_are_named_but_not_inlined(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    content = rehydrate(path)
    assert str(path) in content
    assert "BINARY_PAYLOAD" not in content
    assert "\ufffd" not in content


def test_utf8_text_is_inlined(tmp_path):
    path = tmp_path / "text.txt"
    path.write_text("current text 日本語\n")
    assert "current text 日本語\n" in rehydrate(path)
