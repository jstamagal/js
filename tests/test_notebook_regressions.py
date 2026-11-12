"""Notebook IDs and MIME summaries retain their native meaning."""

import base64
import json

import pytest

from js.toolkit import ToolContext, call_tool, notebook
from js.toolkit.registry import build_default_registry


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


def test_inserted_id_stays_stable_with_malformed_notebook_version(tmp_path):
    path = tmp_path / "malformed.ipynb"
    path.write_text(json.dumps({
        "nbformat": "four", "nbformat_minor": "broken", "metadata": {},
        "cells": [{"cell_type": "markdown", "id": "existing", "source": "old", "metadata": {}}],
    }))
    context = ToolContext(cwd=tmp_path)
    registry = build_default_registry()

    def invoke(name, **args):
        result = call_tool(registry.resolve(name), args, context)
        assert not result.startswith("ERROR"), result
        return result

    invoke("read", file_path=str(path))
    invoke("notebook_edit", file_path=str(path), edit_mode="insert", cell_type="markdown", new_source="first")
    first = json.loads(path.read_text())["cells"][0]
    assert first["id"] and first["id"] != "existing"
    invoke("notebook_edit", file_path=str(path), edit_mode="insert", cell_type="markdown", new_source="second")
    cells = json.loads(path.read_text())["cells"]
    assert [cell["source"] for cell in cells] == ["second", "first", "old"]
    assert cells[1]["id"] == first["id"]
    assert len({cell["id"] for cell in cells}) == 3
