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


def test_sparse_oversized_file_never_uses_unbounded_read(tmp_path, monkeypatch):
    from js.toolkit import fs

    path = tmp_path / "large.log"
    with path.open("wb") as handle:
        handle.write(b"x" * 4096)
        handle.truncate(1024 * 1024 * 1024)
    original = fs._read_regular_bytes
    limits = []

    def bounded(path, limit=None):
        limits.append(limit)
        assert limit is not None
        return original(path, limit)

    monkeypatch.setattr(fs, "_read_regular_bytes", bounded)
    assert str(path) in rehydrate(path, per_file_tokens=10)
    assert limits and max(limits) <= 4096


def test_text_growth_after_stat_still_uses_bounded_read(tmp_path, monkeypatch):
    from js.toolkit import fs

    path = tmp_path / "growth.log"
    path.write_text("x")
    original = fs._read_regular_bytes
    limits = []

    def grow(target, limit=None):
        limits.append(limit)
        assert limit is not None
        if len(limits) == 2:
            target.write_text("LARGE_PAYLOAD" * 10000)
        return original(target, limit)

    monkeypatch.setattr(fs, "_read_regular_bytes", grow)
    content = rehydrate(path, per_file_tokens=10)
    assert "LARGE_PAYLOAD" not in content
    assert limits == [4096, 173]


@pytest.mark.parametrize("text", ["x" * 43, "日" * 43, "😀" * 43])
def test_multibyte_utf8_uses_character_budget_and_preserves_flooring(tmp_path, text):
    path = tmp_path / "unicode.txt"
    path.write_text(text)
    assert text in rehydrate(path, per_file_tokens=10)


def test_shared_budget_omission_is_not_a_per_file_size_omission(tmp_path):
    import os
    from js import compaction

    first, second, tiny = [tmp_path / name for name in ("first.txt", "second.txt", "tiny.txt")]
    first.write_text("a" * 40)
    second.write_text("b" * 40)
    tiny.write_text("c" * 4)
    for path, mtime in [(first, 30), (second, 20), (tiny, 10)]:
        os.utime(path, (mtime, mtime))
    content = rehydrate(first, second, tiny, per_file_tokens=20, token_budget=11)
    assert "a" * 40 in content
    assert "b" * 40 not in content
    assert "c" * 4 in content
    assert compaction._PROMPTS['post_compact_rehydration_budget_exhausted'].format(path=second) in content
    assert compaction._PROMPTS['post_compact_rehydration_too_large_to_re_attach_read'].format(path=second) not in content


def test_per_file_size_reason_remains_separate(tmp_path):
    from js import compaction

    path = tmp_path / "oversize.txt"
    path.write_text("x" * 100)
    content = rehydrate(path, per_file_tokens=1, token_budget=100)
    assert compaction._PROMPTS['post_compact_rehydration_too_large_to_re_attach_read'].format(path=path) in content
    assert compaction._PROMPTS['post_compact_rehydration_budget_exhausted'].format(path=path) not in content


def test_utf8_text_is_inlined(tmp_path):
    path = tmp_path / "text.txt"
    path.write_text("current text 日本語\n")
    assert "current text 日本語\n" in rehydrate(path)
