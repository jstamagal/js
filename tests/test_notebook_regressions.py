"""Notebook IDs and MIME summaries retain their native meaning."""

import base64

import pytest

from js.toolkit import notebook


@pytest.mark.parametrize("requested, expected", [(" x", 0), ("x", 1), (" ", 2), (" x ", 1), ("missing", None)])
def test_cell_lookup_prefers_exact_id_before_whitespace_fallback(requested, expected):
    cells = [{"id": " x"}, {"id": "x"}, {"id": " "}]
    assert notebook.find_cell(cells, requested) == expected


@pytest.mark.parametrize("mime, payload, size", [
    ("text/latex", "$$x^2$$" * 400, "2.7 KB"),
    ("text/markdown", ["é", "文"], "5 B"),
    ("application/javascript", "alert(1)", "8 B"),
    ("image/svg+xml", "<svg>é</svg>", "13 B"),
    ("application/json", {"é": [True, None]}, "20 B"),
    ("image/png", base64.b64encode(b"ab").decode(), "2 B"),
    ("application/pdf", base64.b64encode(b"abcde").decode(), "5 B"),
])
def test_output_summary_sizes_by_mime(mime, payload, size):
    [heading] = notebook.summarize_output({"output_type": "display_data", "data": {mime: payload}}, 10)
    assert f"{mime} ({size})" in heading
