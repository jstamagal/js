"""Content-bearing tool results retain value semantics through runtime limits."""

import pytest

from js import runtime
from js.toolkit.core import Output
from js.toolkit.presentation import result_text
from js.toolkit.results import summary_text


@pytest.mark.parametrize("content", ["ERROR disk full\nsecond line", "  ERROR is a log entry"])
def test_output_content_is_not_an_error(content):
    result = Output(content)
    tracker = runtime.ToolErrorTracker(limit=2)
    tracker.record("read", "ERROR: missing file")
    for _ in range(3):
        assert tracker.record("read", result) is result
    assert not tracker.errors
    assert not tracker.limit_reached()
    assert result_text(result) == (content, False)
    assert summary_text(result) == (content, False)


@pytest.mark.parametrize("cap", [0, 40])
def test_output_semantics_survive_result_caps(cap):
    result = Output("ERROR " + "log line\n" * 100)
    capped = runtime._cap_result(result, cap, inline_cap=0)
    assert isinstance(capped, Output)
    assert not result_text(capped)[1]
    batch = runtime._cap_batch_results([result], 40)
    assert isinstance(batch[0], Output)
    assert not runtime.ToolErrorTracker(limit=1).record("read", batch[0]).endswith("</retry>")


@pytest.mark.parametrize("content", ["ERROR " + "line\n" * 1000, "ERROR \udc80 malformed log"])
def test_output_semantics_survive_spill_and_sanitizing(content, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.paths, "tool_results_dir", lambda: tmp_path)
    capped = runtime._cap_result(Output(content), 2000, inline_cap=1000)
    assert isinstance(capped, Output)
    assert not result_text(capped)[1]
    tracker = runtime.ToolErrorTracker(limit=1)
    assert tracker.record("read", capped) is capped
    assert not tracker.limit_reached()


def test_actual_error_still_counts():
    tracker = runtime.ToolErrorTracker(limit=1)
    result = tracker.record("read", "ERROR: missing file")
    assert result.endswith("</retry>")
    assert tracker.limit_reached()
    assert result_text("ERROR: missing file")[1]
    assert summary_text("ERROR: missing file")[1]
