"""Notebooks: `read` shows an .ipynb as cells, `notebook_edit` changes a cell by
id under the read tool's stale-read guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from js.toolkit import ToolContext, call_tool
from js.toolkit.registry import build_default_registry

PNG_PAYLOAD = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk" * 40


def tool(name: str, context: ToolContext, **args) -> str:
    return call_tool(build_default_registry().resolve(name), args, context)


def modern_notebook() -> dict:
    return {
        "cells": [
            {"cell_type": "code", "execution_count": 3, "id": "a1", "metadata": {},
             "outputs": [
                 {"name": "stdout", "output_type": "stream", "text": [f"line {n}\n" for n in range(1, 31)]},
                 {"data": {"image/png": PNG_PAYLOAD, "text/plain": ["<Figure size 640x480>"]},
                  "execution_count": 3, "metadata": {}, "output_type": "execute_result"},
             ],
             "source": ["import math\n", "print(math.pi)"]},
            {"cell_type": "markdown", "id": "b2", "metadata": {}, "source": ["# Title\n", "prose"]},
            {"cell_type": "code", "execution_count": 4, "id": "c3", "metadata": {},
             "outputs": [{"ename": "NameError", "evalue": "name 'nope' is not defined",
                          "output_type": "error", "traceback": ["\x1b[0;31mTraceback\x1b[0m"]}],
             "source": ["nope"]},
        ],
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                     "language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def old_notebook() -> dict:
    notebook = modern_notebook()
    notebook["nbformat_minor"] = 2
    for cell in notebook["cells"]:
        del cell["id"]
    return notebook


def write_notebook(path: Path, notebook: dict) -> None:
    path.write_text(json.dumps(notebook, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def cells_of(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cells"]


@pytest.fixture
def nb(tmp_path):
    path = tmp_path / "analysis.ipynb"
    write_notebook(path, modern_notebook())
    return path, ToolContext(cwd=tmp_path)


def test_read_shows_cells_with_ids_and_summarised_outputs(nb):
    path, context = nb

    view = tool("read", context, file_path=str(path))

    for cell_id in ("a1", "b2", "c3"):
        assert f"id={cell_id}" in view
    assert "print(math.pi)" in view
    assert "# Title" in view
    assert "line 1" in view
    assert "line 30" not in view
    assert "image/png" in view
    assert PNG_PAYLOAD[:40] not in view
    assert "<Figure size 640x480>" in view
    assert "NameError" in view
    assert "\x1b" not in view
    assert '"cell_type"' not in view


def test_notebook_output_lines_setting_bounds_each_output(nb):
    path, context = nb
    context.notebook_output_lines = 30

    view = tool("read", context, file_path=str(path))

    assert "line 30" in view


def test_ranged_read_returns_the_raw_json(nb):
    path, context = nb

    raw = tool("read", context, file_path=str(path), range={"start_line": 1, "end_line": 5})

    assert '"cells"' in raw


def test_a_file_that_is_not_a_notebook_reads_as_text(tmp_path):
    path = tmp_path / "broken.ipynb"
    path.write_text("not json at all\n")

    result = tool("read", ToolContext(cwd=tmp_path), file_path=str(path))

    assert "not json at all" in result


def test_replace_sets_source_and_clears_outputs(nb):
    path, context = nb
    tool("read", context, file_path=str(path))

    result = tool("notebook_edit", context, file_path=str(path), cell_id="a1", new_source="x = 1\ny = 2")

    assert not result.startswith("ERROR"), result
    cells = cells_of(path)
    assert "".join(cells[0]["source"]) == "x = 1\ny = 2"
    assert cells[0]["outputs"] == []
    assert cells[0]["execution_count"] is None
    assert cells[0]["id"] == "a1"
    assert cells[2]["outputs"][0]["ename"] == "NameError"
    assert cells[1] == modern_notebook()["cells"][1]
    assert path.read_text().startswith('{\n "cells": [')


def test_replace_can_change_a_cell_type(nb):
    path, context = nb
    tool("read", context, file_path=str(path))

    tool("notebook_edit", context, file_path=str(path), cell_id="c3", new_source="plain words", cell_type="markdown")
    tool("notebook_edit", context, file_path=str(path), cell_id="b2", new_source="1 + 1", cell_type="code")

    cells = cells_of(path)
    assert cells[2]["cell_type"] == "markdown"
    assert "outputs" not in cells[2] and "execution_count" not in cells[2]
    assert cells[1]["cell_type"] == "code"
    assert cells[1]["outputs"] == [] and cells[1]["execution_count"] is None


def test_insert_after_a_cell_and_at_the_top(nb):
    path, context = nb
    tool("read", context, file_path=str(path))

    tool("notebook_edit", context, file_path=str(path), cell_id="a1", new_source="## Notes", cell_type="markdown", edit_mode="insert")
    tool("notebook_edit", context, file_path=str(path), new_source="import os", cell_type="code", edit_mode="insert")

    cells = cells_of(path)
    assert [cell.get("id") for cell in cells][1:3] == ["a1", cells[2]["id"]]
    assert cells[2]["cell_type"] == "markdown" and "".join(cells[2]["source"]) == "## Notes"
    assert "".join(cells[0]["source"]) == "import os"
    assert cells[0]["outputs"] == [] and cells[0]["execution_count"] is None
    ids = [cell["id"] for cell in cells]
    assert len(set(ids)) == len(ids) == 5


def test_delete_by_id(nb):
    path, context = nb
    tool("read", context, file_path=str(path))

    tool("notebook_edit", context, file_path=str(path), cell_id="b2", edit_mode="delete")

    assert [cell["id"] for cell in cells_of(path)] == ["a1", "c3"]


def test_notebook_without_ids_uses_positions(tmp_path):
    path = tmp_path / "old.ipynb"
    write_notebook(path, old_notebook())
    context = ToolContext(cwd=tmp_path)

    view = tool("read", context, file_path=str(path))
    tool("notebook_edit", context, file_path=str(path), cell_id="cell-2", new_source="fixed")
    tool("notebook_edit", context, file_path=str(path), cell_id="cell-0", new_source="text", cell_type="markdown", edit_mode="insert")

    assert "id=cell-0" in view and "id=cell-2" in view
    cells = cells_of(path)
    assert "".join(cells[3]["source"]) == "fixed"
    assert "".join(cells[1]["source"]) == "text"
    assert all("id" not in cell for cell in cells)


def test_unknown_cell_id_is_refused_and_lists_the_ids(nb):
    path, context = nb
    tool("read", context, file_path=str(path))
    before = path.read_bytes()

    result = tool("notebook_edit", context, file_path=str(path), cell_id="zz", new_source="x")

    assert result.startswith("ERROR")
    assert "a1" in result and "c3" in result
    assert path.read_bytes() == before


def test_edit_without_a_read_is_refused(nb):
    path, context = nb
    before = path.read_bytes()

    result = tool("notebook_edit", context, file_path=str(path), cell_id="a1", new_source="x")

    assert result.startswith("ERROR")
    assert path.read_bytes() == before


def test_edit_after_the_notebook_changed_on_disk_is_refused(nb):
    path, context = nb
    tool("read", context, file_path=str(path))
    changed = modern_notebook()
    changed["cells"][1]["source"] = ["edited elsewhere"]
    write_notebook(path, changed)
    outside = path.read_bytes()

    refused = tool("notebook_edit", context, file_path=str(path), cell_id="a1", new_source="x")

    assert refused.startswith("ERROR")
    assert path.read_bytes() == outside
    tool("read", context, file_path=str(path))
    accepted = tool("notebook_edit", context, file_path=str(path), cell_id="a1", new_source="x")
    assert not accepted.startswith("ERROR"), accepted
    assert cells_of(path)[1]["source"] == ["edited elsewhere"]


def test_edits_in_a_row_need_one_read(nb):
    path, context = nb
    tool("read", context, file_path=str(path))

    first = tool("notebook_edit", context, file_path=str(path), cell_id="a1", new_source="one")
    second = tool("notebook_edit", context, file_path=str(path), cell_id="c3", new_source="two")

    assert not first.startswith("ERROR") and not second.startswith("ERROR"), (first, second)


def test_a_notebook_view_read_does_not_authorize_a_raw_patch(nb):
    path, context = nb
    tool("read", context, file_path=str(path))

    result = tool("patch", context, file_path=str(path), old_string='"nbformat": 4', new_string='"nbformat": 5')

    assert result.startswith("ERROR")


def test_undo_restores_the_notebook(nb):
    path, context = nb
    before = path.read_bytes()
    tool("read", context, file_path=str(path))
    tool("notebook_edit", context, file_path=str(path), cell_id="a1", new_source="x")

    tool("undo", context, path=str(path))

    assert path.read_bytes() == before


def test_edit_refuses_a_file_that_is_not_a_notebook(tmp_path):
    path = tmp_path / "plain.py"
    path.write_text("x = 1\n")
    context = ToolContext(cwd=tmp_path)
    tool("read", context, file_path=str(path))

    assert tool("notebook_edit", context, file_path=str(path), cell_id="cell-0", new_source="y").startswith("ERROR")
    assert path.read_text() == "x = 1\n"


def test_insert_needs_a_cell_type(nb):
    path, context = nb
    tool("read", context, file_path=str(path))

    assert tool("notebook_edit", context, file_path=str(path), new_source="x", edit_mode="insert").startswith("ERROR")
