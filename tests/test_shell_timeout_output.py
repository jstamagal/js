"""A shell command that outlives the wait comes back as a handle, not a corpse.

The wait window is how long the CALL blocks; it is not a deadline for the
command. A build that needs five minutes keeps running, the model gets what it
printed so far plus a handle, and comes back for the rest.
"""
from __future__ import annotations

from js.toolkit import ToolContext
from js.toolkit.process_net import shell


def test_command_outliving_the_wait_returns_a_handle_with_output_so_far(tmp_path):
    context = ToolContext(cwd=tmp_path)
    result = shell(
        "printf 'IMPORTANT_PROGRESS_LINE\\n'; sleep 1; printf 'LATE_LINE\\n'; exit 7",
        timeout=1,
        context=context,
    )
    assert "HANDLE" in result and "RUNNING" in result
    assert "IMPORTANT_PROGRESS_LINE" in result
    assert "LATE_LINE" not in result
    handle = result.split("handle ", 1)[1].split(",", 1)[0]

    finished = shell(action="wait", handle=handle, timeout=10, context=context)
    assert "exit=7" in finished
    assert "LATE_LINE" in finished
    # Already-delivered output is not repeated on the follow-up.
    assert "IMPORTANT_PROGRESS_LINE" not in finished


def test_poll_returns_new_output_without_blocking(tmp_path):
    context = ToolContext(cwd=tmp_path)
    started = shell("printf 'one\\n'; sleep 30", timeout=1, context=context)
    handle = started.split("handle ", 1)[1].split(",", 1)[0]
    assert "one" in started

    polled = shell(action="poll", handle=handle, context=context)
    assert "RUNNING" in polled
    assert "one" not in polled

    killed = shell(action="kill", handle=handle, context=context)
    assert killed.startswith("killed handle")
    assert "exit=-9" in killed


def test_handle_defaults_to_the_latest_running_job(tmp_path):
    context = ToolContext(cwd=tmp_path)
    shell("sleep 30", timeout=1, context=context)
    killed = shell(action="kill", context=context)
    assert killed.startswith("killed handle")


def test_clipped_output_still_reports_truncation(tmp_path):
    context = ToolContext(
        cwd=tmp_path,
        max_bash_output_bytes=64,
        max_bash_output_ceiling=32,
    )
    result = shell("head -c 4096 /dev/zero | tr '\\0' x", timeout=10, context=context)
    assert "exit=0" in result
    assert "[truncated: limits.max_bash_output_bytes (32) reached]" in result
    assert "x" * 33 not in result


def test_unknown_action_and_missing_command_are_errors(tmp_path):
    context = ToolContext(cwd=tmp_path)
    assert shell(action="dance", context=context).startswith("ERROR: unknown action")
    assert shell(context=context).startswith("ERROR: command is required")
    assert shell(action="poll", handle="no-such", context=context).startswith("ERROR: no shell job")
