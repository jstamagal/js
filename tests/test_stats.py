from __future__ import annotations

import csv
import json

import pytest

from js import stats


@pytest.mark.parametrize("format", ["json", "csv"])
def test_write_report_with_long_destination_name(tmp_path, format):
    path = tmp_path / ("s" * 245 + f".{format}")
    path.write_text("old report", encoding="utf-8")
    row = {"name": "complete", "prompt": "hello"}

    if format == "json":
        stats.write_json(path, row)
        assert json.loads(path.read_text()) == row
    else:
        stats.write_csv(path, [row])
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 1
        assert rows[0]["name"] == row["name"]
        assert rows[0]["prompt"] == row["prompt"]
