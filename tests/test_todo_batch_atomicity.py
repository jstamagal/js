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
