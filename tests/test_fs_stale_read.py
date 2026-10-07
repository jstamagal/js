"""write(overwrite=true) needs one read; an edit of a file changed on disk since
the read is refused with the hash mismatch named and the diff handed over."""
from __future__ import annotations

from js.toolkit import fs
from js.toolkit.core import ToolContext


def _lines(count: int, fmt: str = "line {n}") -> str:
    return "".join(fmt.format(n=n) + "\n" for n in range(1, count + 1))


def test_overwriting_a_10k_line_file_needs_one_read(tmp_path):
    target = tmp_path / "big.txt"
    target.write_text(_lines(10_000), encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_read_lines=2_000)

    fs.fs_read(file_path=str(target), context=context)
    assert target not in context.fully_read_paths
    result = fs.write(file_path=str(target), content="fresh\n", overwrite=True, context=context)

    assert result.startswith("wrote "), result
    assert target.read_text(encoding="utf-8") == "fresh\n"
    assert fs.undo(str(target), context=context).startswith("restored ")
    assert target.read_text(encoding="utf-8") == _lines(10_000)


def test_overwrite_still_needs_a_read(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("old\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = fs.write(file_path=str(target), content="new\n", overwrite=True, context=context)

    assert result.startswith("ERROR")
    assert target.read_text(encoding="utf-8") == "old\n"


def test_overwrite_of_a_file_changed_since_the_read_is_refused_naming_both_hashes(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("as read\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    fs.fs_read(file_path=str(target), context=context)
    read_hash = fs._hash_bytes(b"as read\n")
    target.write_text("moved underneath\n", encoding="utf-8")
    now_hash = fs._hash_bytes(b"moved underneath\n")

    result = fs.write(file_path=str(target), content="mine\n", overwrite=True, context=context)

    assert result.startswith("ERROR")
    assert read_hash in result and now_hash in result
    assert target.read_text(encoding="utf-8") == "moved underneath\n"


def test_hash_mismatch_is_named_when_no_copy_of_the_read_is_held(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("as read\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    fs.fs_read(file_path=str(target), context=context)
    context.known_content.clear()
    target.write_text("moved underneath\n", encoding="utf-8")

    result = fs.patch(file_path=str(target), old_string="moved", new_string="kept", context=context)

    assert result.startswith("ERROR")
    assert fs._hash_bytes(b"as read\n") in result
    assert fs._hash_bytes(b"moved underneath\n") in result
    assert target.read_text(encoding="utf-8") == "moved underneath\n"


def test_patch_after_an_outside_change_gets_the_diff_and_retries_without_a_read(tmp_path):
    target = tmp_path / "src.py"
    target.write_text(_lines(40), encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    fs.fs_read(file_path=str(target), context=context)
    target.write_text(_lines(40).replace("line 5\n", "line five, edited elsewhere\n"), encoding="utf-8")

    refused = fs.patch(file_path=str(target), old_string="line 30\n", new_string="line thirty\n", context=context)
    retried = fs.patch(file_path=str(target), old_string="line 30\n", new_string="line thirty\n", context=context)
    changed_line = fs.patch(
        file_path=str(target), old_string="line five, edited elsewhere\n", new_string="line 5 again\n", context=context
    )

    assert refused.startswith("ERROR")
    assert "-line 5\n" in refused and "+line five, edited elsewhere\n" in refused
    assert retried.startswith("patched "), retried
    assert changed_line.startswith("patched "), changed_line
    text = target.read_text(encoding="utf-8")
    assert "line thirty\n" in text and "line 5 again\n" in text


def test_the_diff_does_not_grant_lines_the_model_never_saw(tmp_path):
    target = tmp_path / "src.py"
    target.write_text(_lines(100), encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    fs.fs_read(file_path=str(target), start_line=1, end_line=10, context=context)
    target.write_text(_lines(100).replace("line 50\n", "line fifty\n"), encoding="utf-8")

    refused = fs.patch(file_path=str(target), old_string="line 3\n", new_string="line three\n", context=context)
    unseen = fs.patch(file_path=str(target), old_string="line 80\n", new_string="line eighty\n", context=context)
    seen = fs.patch(file_path=str(target), old_string="line 3\n", new_string="line three\n", context=context)
    shown_by_diff = fs.patch(file_path=str(target), old_string="line fifty\n", new_string="line 50\n", context=context)

    assert refused.startswith("ERROR")
    assert unseen.startswith("ERROR")
    assert seen.startswith("patched "), seen
    assert shown_by_diff.startswith("patched "), shown_by_diff
    assert "line 80\n" in target.read_text(encoding="utf-8")


def _rewrite_lines_100_to_180_outside_js(tmp_path, **context_kwargs):
    target = tmp_path / "src.py"
    original = _lines(200)
    target.write_text(original, encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_tool_result_inline_bytes=2_000, **context_kwargs)
    fs.fs_read(file_path=str(target), context=context)
    changed = original.replace("line 1\n", "L1\n")
    for n in range(100, 181):
        changed = changed.replace(f"line {n}\n", f"rewritten outside js {n}\n")
    target.write_text(changed, encoding="utf-8")
    return target, context, changed


def test_a_diff_over_the_result_budget_is_withheld_and_a_short_edit_stays_gated_on_it(tmp_path):
    target, context, _changed = _rewrite_lines_100_to_180_outside_js(tmp_path)

    refused = fs.patch(file_path=str(target), old_string="line 190\n", new_string="x\n", context=context)
    unchanged_seen = fs.patch(file_path=str(target), old_string="line 190\n", new_string="x\n", context=context)
    # Short enough that the exact match is not its own proof, so the withheld
    # diff keeps the edit gated until those lines are read.
    changed_unshown = fs.patch(file_path=str(target), old_string="js 150\n", new_string="y\n", context=context)
    fs.fs_read(file_path=str(target), start_line=100, end_line=180, context=context)
    changed_read = fs.patch(file_path=str(target), old_string="js 150\n", new_string="y\n", context=context)

    assert refused.startswith("ERROR")
    assert "rewritten outside js" not in refused
    assert unchanged_seen.startswith("patched "), unchanged_seen
    assert changed_unshown.startswith("ERROR")
    assert changed_read.startswith("patched "), changed_read


def test_an_overwrite_retry_after_a_withheld_diff_discards_the_change_and_undo_restores_it(tmp_path):
    target, context, changed = _rewrite_lines_100_to_180_outside_js(tmp_path)

    refused = fs.write(file_path=str(target), content="mine\n", overwrite=True, context=context)
    assert refused.startswith("ERROR")
    assert target.read_text(encoding="utf-8") == changed
    retried = fs.write(file_path=str(target), content="mine\n", overwrite=True, context=context)

    assert retried.startswith("wrote "), retried
    assert target.read_text(encoding="utf-8") == "mine\n"
    assert fs.undo(str(target), context=context).startswith("restored ")
    assert target.read_text(encoding="utf-8") == changed


def test_after_a_partial_read_overwrite_the_new_content_is_editable(tmp_path):
    target = tmp_path / "big.txt"
    target.write_text(_lines(5_000), encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_read_lines=100)
    fs.fs_read(file_path=str(target), context=context)
    fs.write(file_path=str(target), content=_lines(3_000, "new {n}"), overwrite=True, context=context)

    result = fs.patch(file_path=str(target), old_string="new 2999\n", new_string="edited\n", context=context)

    assert result.startswith("patched "), result
