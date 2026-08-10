"""A timed-out command must hand back what it managed to print.

A build that emitted 200 lines and then hung used to come back as a bare
"ERROR: command timed out after Ns" — the last lines before the hang are the
whole diagnosis, and they were being discarded.
"""
from __future__ import annotations

from js.toolkit import ToolContext
from js.toolkit.process_net import shell


def test_output_written_before_the_hang_survives_the_timeout(tmp_path):
    context = ToolContext(cwd=tmp_path)
    result = shell(
        "printf 'IMPORTANT_PROGRESS_LINE\\n'; sleep 30",
        timeout=2,
        context=context,
    )
    assert result.startswith("ERROR:")
    assert "timed out after 2s" in result
    assert "IMPORTANT_PROGRESS_LINE" in result


def test_stderr_written_before_the_hang_survives_too(tmp_path):
    context = ToolContext(cwd=tmp_path)
    result = shell(
        "printf 'FAILED_TO_OPEN_SOCKET\\n' >&2; sleep 30",
        timeout=2,
        context=context,
    )
    assert result.startswith("ERROR:")
    assert "FAILED_TO_OPEN_SOCKET" in result
    assert "stderr" in result


def test_a_silent_hang_says_so_rather_than_showing_an_empty_section(tmp_path):
    context = ToolContext(cwd=tmp_path)
    result = shell("sleep 30", timeout=2, context=context)
    assert result.startswith("ERROR:")
    assert "no output before it was killed" in result


def test_ansi_is_stripped_from_pre_timeout_output_like_it_is_on_success(tmp_path):
    context = ToolContext(cwd=tmp_path)
    result = shell(
        "printf '\\033[31mRED_PROGRESS\\033[0m\\n'; sleep 30",
        timeout=2,
        context=context,
    )
    assert "RED_PROGRESS" in result
    assert "\x1b[" not in result
