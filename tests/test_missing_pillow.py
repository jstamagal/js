"""Without Pillow, terminal_snapshot and browser_probe raise one error, not a traceback."""

import builtins
import shlex
import sys

import pytest

from js.toolkit import browser, terminal
from js.toolkit.core import ToolContext


def without_pillow(monkeypatch):
    original = builtins.__import__

    def without_optional(name, *args, **kwargs):
        if name.split(".")[0] == "PIL":
            raise ModuleNotFoundError(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_optional)


def test_terminal_snapshot_reports_its_install_extra(monkeypatch, tmp_path):
    context = ToolContext(cwd=tmp_path)
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(60)')}"
    try:
        terminal.terminal_session("start", command=command, wait_ms=0, context=context)
        without_pillow(monkeypatch)
        result = terminal.terminal_snapshot(output_path=str(tmp_path / "screen.png"), wait_ms=0, context=context)
    finally:
        monkeypatch.undo()
        terminal.close_terminal_sessions(context)

    # The renderer's own RuntimeError, not the import's ModuleNotFoundError.
    assert result.startswith("ERROR:")
    assert "RuntimeError" in result


def test_browser_frame_metrics_report_their_install_extra(monkeypatch):
    without_pillow(monkeypatch)
    with pytest.raises(RuntimeError):
        browser._frame_metrics(b"")
