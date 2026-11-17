"""A place that fails reports its error the first time only, and a log file
that fails a write is dropped without breaking the writer."""

from __future__ import annotations

import pytest

from js import debug_trace, output, transcript


class _Broken:
    name = "/broken.log"

    def write(self, text):
        raise OSError(28, "No space left on device")

    def flush(self):
        raise OSError(28, "No space left on device")

    def close(self):
        pass


class _Notices:
    def __init__(self):
        self.lines = []

    def emit(self, event):
        if event.name == "notice":
            self.lines.append(dict(event.fields)["fields"]["error"])


@pytest.fixture
def notices(monkeypatch):
    monkeypatch.setattr(output, "_failed_places", set())
    sink = _Notices()
    with output.using_sink(sink):
        yield sink.lines


def test_failed_once_reports_each_place_once(notices):
    output.failed_once("here", ValueError("a"))
    output.failed_once("here", ValueError("b"))
    output.failed_once("there", ValueError("c"))
    assert len(notices) == 2
    assert "ValueError" in notices[0] and "a" in notices[0]


def test_transcript_log_drops_failed_file_and_reports_once(notices, tmp_path):
    good = open(tmp_path / "good.log", "a", encoding="utf-8")
    sink = transcript.TranscriptLogSink([_Broken(), good], [], settings={"TRANSCRIPT_DISABLE_ALL_ON_ERROR": False})
    sink.write_plain("one\n")
    sink.write_plain("two\n")
    sink.close()
    assert len(notices) == 1 and "/broken.log" in notices[0]
    text = (tmp_path / "good.log").read_text()
    assert "one" in text and "two" in text


def test_debug_trace_drops_failed_file_and_reports_once(notices, tmp_path):
    good = open(tmp_path / "good.log", "a", encoding="utf-8")
    sink = debug_trace.DebugTraceSink([_Broken(), good], [])
    sink.write("one\n")
    sink.write("two\n")
    sink.close()
    assert len(notices) == 1 and "/broken.log" in notices[0]
    assert (tmp_path / "good.log").read_text() == "one\ntwo\n"
