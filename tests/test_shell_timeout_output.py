"""A timed-out command must hand back what it managed to print.

A build that emitted 200 lines and then hung used to come back as a bare
"ERROR: command timed out after Ns" — the last lines before the hang are the
whole diagnosis, and they were being discarded.
"""
from __future__ import annotations

import signal

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


def test_clipped_timeout_reports_status_and_truncation(tmp_path):
    context = ToolContext(
        cwd=tmp_path,
        max_bash_output_bytes=64,
        max_bash_output_ceiling=32,
    )
    result = shell(
        "head -c 4096 /dev/zero | tr '\\0' x; sleep 30",
        timeout=1,
        context=context,
    )

    assert f"exit={-signal.SIGKILL}" in result
    assert "ERROR: command timed out after 1s" in result
    assert "x" * 32 in result
    assert "x" * 33 not in result
    assert "[truncated: limits.max_bash_output_bytes (32) reached]" in result


def test_shell_clamps_successful_output_to_configured_ceiling(tmp_path):
    context = ToolContext(
        cwd=tmp_path,
        max_bash_output_bytes=64,
        max_bash_output_ceiling=10,
    )

    result = shell("printf 12345678901234567890", context=context)

    assert "exit=0" in result
    assert "--- stdout ---\n1234567890\n" in result
    assert "12345678901" not in result
    assert "[truncated: limits.max_bash_output_bytes (10) reached]" in result


def test_non_positive_ceiling_leaves_shell_byte_cap_in_effect(tmp_path):
    context = ToolContext(
        cwd=tmp_path,
        max_bash_output_bytes=12,
        max_bash_output_ceiling=0,
    )

    result = shell("printf 12345678901234567890", context=context)

    assert "--- stdout ---\n123456789012\n" in result
    assert "1234567890123" not in result
    assert "[truncated: limits.max_bash_output_bytes (12) reached]" in result


def test_zero_cap_timeout_still_marks_discarded_output(tmp_path):
    context = ToolContext(
        cwd=tmp_path,
        max_bash_output_bytes=0,
        max_bash_output_ceiling=0,
    )

    result = shell("printf discarded; sleep 30", timeout=1, context=context)

    assert f"exit={-signal.SIGKILL}" in result
    assert "[truncated: limits.max_bash_output_bytes (0) reached]" in result


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
