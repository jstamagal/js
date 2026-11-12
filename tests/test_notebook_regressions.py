"""Notebook IDs and MIME summaries retain their native meaning."""

import pytest

from js.toolkit import notebook


@pytest.mark.parametrize("requested, expected", [(" x", 0), ("x", 1), (" ", 2), (" x ", 1), ("missing", None)])
def test_cell_lookup_prefers_exact_id_before_whitespace_fallback(requested, expected):
    cells = [{"id": " x"}, {"id": "x"}, {"id": " "}]
    assert notebook.find_cell(cells, requested) == expected
