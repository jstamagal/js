from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from js import stats


@pytest.mark.parametrize("format", ["json", "csv"])
def test_interleaved_writers_leave_one_complete_report(tmp_path, monkeypatch, format):
    path = tmp_path / f"stats.{format}"
    first = {"name": "first", "prompt": "A" * 1000, "ok": True}
    second = {"name": "second", "prompt": "B", "ok": False}
    nested = False
    if format == "json":
        write_text = Path.write_text

        def interleaved_write(target, text, *args, **kwargs):
            nonlocal nested
            if nested:
                return write_text(target, text, *args, **kwargs)
            with target.open("w", encoding="utf-8") as stream:
                stream.write(text[:len(text) // 2])
                stream.flush()
                nested = True
                stats.write_json(path, second)
                stream.write(text[len(text) // 2:])
            return len(text)

        monkeypatch.setattr(Path, "write_text", interleaved_write)
        stats.write_json(path, first)
        assert json.loads(path.read_text()) in (first, second)
    else:
        first, second = second, first
        writeheader = csv.DictWriter.writeheader

        def interleaved_header(writer):
            nonlocal nested
            result = writeheader(writer)
            if not nested:
                nested = True
                stats.write_csv(path, [second])
            return result

        monkeypatch.setattr(csv.DictWriter, "writeheader", interleaved_header)
        stats.write_csv(path, [first])
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        expected = [{key: str(row.get(key, "")) for key in stats.ROW_FIELDS} for row in (first, second)]
        assert rows in ([expected[0]], [expected[1]])
