"""Search subprocesses stop at the runtime's turn-cancellation boundary."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from js import runtime
from js.toolkit import fs
from js.toolkit.core import Tool, ToolContext
from js.toolkit.registry import _registry_from_tools


def test_dispatch_cancellation_stops_fs_search(monkeypatch, tmp_path):
    ready = tmp_path / "ready"
    binary = tmp_path / "fake-rg"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, time\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    binary.chmod(0o755)
    monkeypatch.setattr(fs, "resolve_binary", lambda name: str(binary))
    monkeypatch.setattr(fs, "_RG_TIMEOUT_S", 2)
    context = ToolContext(cwd=tmp_path)
    registry = _registry_from_tools((Tool(
        "fs_search", "search", fs.fs_search, {"pattern": {"type": "string"}},
    ),))

    async def drive():
        task = asyncio.create_task(runtime._dispatch_batch(
            [runtime._PendingToolCall("1", "fs_search", ['{"pattern":"needle"}'])],
            runtime.Telemetry(debug_log=None), 0, False, runtime.ToolErrorTracker(),
            registry, context, asyncio.get_running_loop(),
        ))
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 1

    asyncio.run(drive())
    pid = int(ready.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert fs.interrupt_inflight(context) == 0


def test_search_interrupt_is_scoped_and_reaps_descendants(tmp_path):
    context = ToolContext(cwd=tmp_path)
    other = ToolContext(cwd=tmp_path)
    ready = tmp_path / "ready"
    script = (
        "import os, pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(ready)!r}).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            fs._rg_stream, [sys.executable, "-c", script], 100, 3, context=context,
        )
        deadline = time.monotonic() + 2
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert fs.interrupt_inflight(other) == 0
        assert not future.done()
        assert fs.interrupt_inflight(context) == 1
        _lines, code, _stderr, timed_out = future.result(timeout=1)
    assert code == -9
    assert not timed_out
    assert fs.interrupt_inflight(context) == 0
    # A killed descendant may briefly remain as an init-owned zombie, but cannot
    # compute or retain any pipe descriptors.
    status = Path(f"/proc/{ready.read_text()}/stat")
    assert not status.exists() or status.read_text().split()[2] == "Z"


def test_search_keyboard_interrupt_reaps_child(monkeypatch, tmp_path):
    popen = subprocess.Popen
    spawned = []
    context = ToolContext(cwd=tmp_path)

    class InterruptedOutput:
        def __init__(self, stream):
            self.stream = stream

        def __iter__(self):
            raise KeyboardInterrupt

        def close(self):
            self.stream.close()

    def start(*args, **kwargs):
        proc = popen(*args, **kwargs)
        spawned.append(proc)
        proc.stdout = InterruptedOutput(proc.stdout)
        return proc

    monkeypatch.setattr(fs.subprocess, "Popen", start)
    try:
        with pytest.raises(KeyboardInterrupt):
            fs._rg_stream(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                want=100, timeout=3, context=context,
            )
        assert spawned[0].poll() is not None
        assert fs.interrupt_inflight(context) == 0
    finally:
        if spawned and spawned[0].poll() is None:
            os.killpg(spawned[0].pid, signal.SIGKILL)
            spawned[0].wait()


def test_search_deadline_survives_closed_stdout():
    lines, code, _stderr, timed_out = fs._rg_stream(
        [sys.executable, "-c", "import os, time; os.close(1); time.sleep(60)"],
        want=100, timeout=0.2,
    )
    assert lines == []
    assert code == -9
    assert timed_out
