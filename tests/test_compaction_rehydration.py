"""Post-compaction file attachment preserves text and reports omitted files."""

import json

import pytest
from test_config_compaction_layers import _compact_test_cfg

from js import compaction, compaction_summary
from js.toolkit import ToolContext

_BUDGETS = {
    "max_files": "COMPACTION_REATTACH_FILES_MAX",
    "token_budget": "COMPACTION_REATTACH_TOKENS_MAX",
    "per_file_tokens": "COMPACTION_REATTACH_FILE_TOKENS_MAX",
}


def compaction_rehydration(monkeypatch, directory, context, **budgets) -> dict | None:
    """The message a forced compaction re-attaches the files `context` read
    in, or None. `budgets` (max_files, token_budget, per_file_tokens) set the
    COMPACTION_REATTACH_* settings."""

    async def summarize(*_args, **_kwargs):
        return "summary"

    monkeypatch.setattr(compaction_summary, "summarize", summarize)
    settings = {"COMPACTION_KEEP_RECENT_TOKENS": 1, **{_BUDGETS[name]: value for name, value in budgets.items()}}
    cfg = _compact_test_cfg(directory / "compaction", settings)
    # The summarized prefix outweighs any re-attachment, so the savings check keeps it.
    messages = [{"role": "user", "content": "old " * 100_000}, {"role": "user", "content": "new"}]
    result = compaction.compact_now_sync(cfg, "SYSTEM", messages, forced=True, context=context)
    assert compaction.compacted(result), result
    marks = [json.loads(line).get("marker", "") for line in cfg.session_file.read_text().splitlines()]
    [mark] = [mark for mark in marks if mark.startswith("compaction:")]
    return json.loads(mark.split(":", 1)[1]).get("rehydrated")


@pytest.fixture
def rehydrate(monkeypatch, tmp_path):
    """`rehydrate(*paths, **budgets)`: the text a forced compaction re-attaches
    after `paths` were read, or "" when it re-attaches nothing."""

    def run(*paths, **budgets):
        context = ToolContext()
        for path in paths:
            context.reads.remember(path, "read")
        message = compaction_rehydration(monkeypatch, tmp_path, context, **budgets)
        return message["content"] if message else ""

    return run


@pytest.mark.parametrize("name,data", [
    ("shot.png", b"\x89PNG\r\n\x1a\n\x00BINARY_PAYLOAD"),
    ("paper.pdf", b"%PDF-1.7\nBINARY_PAYLOAD"),
    ("blob.dat", b"\x00BINARY_PAYLOAD"),
    ("bad.txt", b"\xffBINARY_PAYLOAD"),
])
def test_nontext_files_are_named_but_not_inlined(tmp_path, name, data, rehydrate):
    path = tmp_path / name
    path.write_bytes(data)
    content = rehydrate(path)
    assert str(path) in content
    assert "BINARY_PAYLOAD" not in content
    assert "\ufffd" not in content


def test_sparse_oversized_file_never_uses_unbounded_read(tmp_path, monkeypatch, rehydrate):
    from js.toolkit import fs

    path = tmp_path / "large.log"
    with path.open("wb") as handle:
        handle.write(b"x" * 4096)
        handle.truncate(1024 * 1024 * 1024)
    original = fs.read_regular_bytes
    limits = []

    def bounded(path, limit=None):
        limits.append(limit)
        assert limit is not None
        return original(path, limit)

    monkeypatch.setattr(fs, "read_regular_bytes", bounded)
    assert str(path) in rehydrate(path, per_file_tokens=10)
    assert limits and max(limits) <= 4096


def test_text_growth_after_stat_still_uses_bounded_read(tmp_path, monkeypatch, rehydrate):
    from js.toolkit import fs

    path = tmp_path / "growth.log"
    path.write_text("x")
    original = fs.read_regular_bytes
    limits = []

    def grow(target, limit=None):
        limits.append(limit)
        assert limit is not None
        if len(limits) == 2:
            target.write_text("LARGE_PAYLOAD" * 10000)
        return original(target, limit)

    monkeypatch.setattr(fs, "read_regular_bytes", grow)
    content = rehydrate(path, per_file_tokens=10)
    assert "LARGE_PAYLOAD" not in content
    assert limits == [4096, 173]


@pytest.mark.parametrize("text", ["x" * 43, "日" * 43, "😀" * 43])
def test_multibyte_utf8_uses_character_budget_and_preserves_flooring(tmp_path, text, rehydrate):
    path = tmp_path / "unicode.txt"
    path.write_text(text)
    assert text in rehydrate(path, per_file_tokens=10)


def test_shared_budget_omission_is_not_a_per_file_size_omission(tmp_path, rehydrate):
    import os

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
    assert f'### {second}\n(not re-attached because the shared file budget is exhausted; read it if you need it)' in content
    assert f'### {second}\n(too large to re-attach; read it if you need it)' not in content


def test_per_file_size_reason_remains_separate(tmp_path, rehydrate):

    path = tmp_path / "oversize.txt"
    path.write_text("x" * 100)
    content = rehydrate(path, per_file_tokens=1, token_budget=100)
    assert f'### {path}\n(too large to re-attach; read it if you need it)' in content
    assert f'### {path}\n(not re-attached because the shared file budget is exhausted; read it if you need it)' not in content


def test_vanished_file_is_named_without_inline_contents(tmp_path, rehydrate):
    path = tmp_path / "vanished.txt"
    assert str(path) in rehydrate(path)


def test_unreadable_file_is_named_without_inline_contents(tmp_path, monkeypatch, rehydrate):
    from js.toolkit import fs

    path = tmp_path / "private.txt"
    path.write_text("UNREADABLE_PAYLOAD")

    def unreadable(*args, **kwargs):
        raise PermissionError("cannot read")

    monkeypatch.setattr(fs, "read_regular_bytes", unreadable)
    content = rehydrate(path)
    assert str(path) in content
    assert "UNREADABLE_PAYLOAD" not in content


def test_fifo_is_named_without_blocking(tmp_path, rehydrate):
    import os

    path = tmp_path / "pipe.txt"
    os.mkfifo(path)
    assert str(path) in rehydrate(path)


def test_utf8_text_is_inlined(tmp_path, rehydrate):
    path = tmp_path / "text.txt"
    path.write_text("current text 日本語\n")
    assert "current text 日本語\n" in rehydrate(path)
