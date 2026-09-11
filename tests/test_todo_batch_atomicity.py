"""todo_write validates the whole batch before applying any of it.

Rejecting item 3 after items 1-2 had already landed left the model holding an
ERROR and a silently half-updated list.
"""
from __future__ import annotations

from js.toolkit import ToolContext
from js.toolkit.meta import todo_read, todo_write


def _batch(*pairs):
    return [{"content": content, "status": status} for content, status in pairs]


def test_a_bad_status_late_in_the_batch_applies_none_of_it():
    context = ToolContext()
    result = todo_write(
        _batch(("first", "completed"), ("second", "pending"), ("third", "banana")),
        context=context,
    )
    assert result.startswith("ERROR:")
    assert "banana" in result
    assert todo_read(context=context) == "No todos."


def test_an_empty_content_late_in_the_batch_applies_none_of_it():
    context = ToolContext()
    result = todo_write(
        _batch(("first", "pending"), ("second", "pending"), ("   ", "pending")),
        context=context,
    )
    assert result.startswith("ERROR:")
    assert todo_read(context=context) == "No todos."


def test_a_rejected_batch_leaves_previously_stored_todos_untouched():
    context = ToolContext()
    todo_write(_batch(("keep me", "in_progress")), context=context)

    result = todo_write(
        _batch(("added", "pending"), ("bad", "not-a-status")),
        context=context,
    )
    assert result.startswith("ERROR:")
    listing = todo_read(context=context)
    assert "keep me" in listing
    assert "added" not in listing


def test_a_valid_batch_still_applies_every_item():
    context = ToolContext()
    result = todo_write(
        _batch(("one", "completed"), ("two", "in_progress"), ("three", "pending")),
        context=context,
    )
    assert not result.startswith("ERROR:")
    listing = todo_read(context=context)
    for name in ("one", "two", "three"):
        assert name in listing


# ── Issue #43: empty batches and unknown-key cancels must not report success ──


def test_an_empty_batch_is_reported_as_no_change():
    context = ToolContext()
    result = todo_write([], context=context)
    assert "unchanged" in result
    assert "empty batch" in result
    assert todo_read(context=context) == "No todos."


def test_an_empty_batch_with_existing_todos_reports_no_change():
    context = ToolContext()
    todo_write(_batch(("keep me", "in_progress")), context=context)

    result = todo_write([], context=context)
    assert "unchanged" in result
    listing = todo_read(context=context)
    assert "keep me" in listing


def test_cancelling_an_unknown_key_reports_not_found_and_changes_nothing():
    context = ToolContext()
    todo_write(_batch(("real", "pending")), context=context)

    result = todo_write(_batch(("ghost", "cancelled")), context=context)
    assert result.startswith("ERROR:")
    assert "not found" in result
    listing = todo_read(context=context)
    assert "real" in listing
    assert "ghost" not in listing


def test_add_then_cancel_within_one_batch_still_works():
    context = ToolContext()
    result = todo_write(
        _batch(("transient", "pending"), ("transient", "cancelled")),
        context=context,
    )
    assert not result.startswith("ERROR:")
    assert todo_read(context=context) == "No todos."
