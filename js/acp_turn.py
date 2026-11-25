"""Run an ACP prompt through the installed js headless entry point."""

import asyncio
import io
import json
import os
import signal
import sys
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from .capped_process import descendants

Emit = Callable[[dict], Awaitable[None]]
Approve = Callable[[dict], Awaitable[str | None]]


class TurnError(RuntimeError):
    """The worker could not complete the turn."""


async def _write_input(stream, payload: bytes) -> None:
    try:
        stream.write(payload)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass


async def _stderr_tail(stream) -> str:
    tail = b""
    while chunk := await stream.read(4096):
        tail = (tail + chunk)[-4096:]
    return tail.decode("utf-8", errors="replace").strip()


def _kill_descendants(descendants: set[int]) -> None:
    for pid in descendants:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


async def _stop(process, stderr: asyncio.Task) -> None:
    process.stdin.close()
    children = {pid for pid, _ in descendants(process.pid)}
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    # Drain pipes while the worker handles SIGTERM and saves the session.
    async def drain_stdout():
        while await process.stdout.read(65536):
            pass

    drain = asyncio.create_task(drain_stdout())
    try:
        await asyncio.wait({drain, stderr}, timeout=1)
    finally:
        children.update(pid for pid, _ in descendants(process.pid))
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        _kill_descendants(children)
        await process.wait()
        await asyncio.gather(drain, stderr)


async def run_turn(
    *,
    cwd: Path,
    session_id: str,
    text: str,
    options: list[str],
    mcp_servers: dict,
    emit: Emit,
    approve: Approve | None = None,
) -> None:
    """Emit js JSON-stream records for a saved session. Cancel the task to stop."""
    payload = _json_line({"prompt": text, "mcp_servers": mcp_servers,
                          "approval": approve is not None})
    launch = asyncio.create_task(asyncio.create_subprocess_exec(
        sys.executable, "-I", "-m", "js.acp_turn",
        *options, "--session=" + session_id, "--json-stream", "-p", "-",
        cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=16 * 1024 * 1024,
    ))
    try:
        process = await asyncio.shield(launch)
    except asyncio.CancelledError:
        process = await launch
        await _stop(process, asyncio.create_task(_stderr_tail(process.stderr)))
        raise
    except OSError as exc:
        raise TurnError(f"Could not start js worker: {exc}") from exc

    stderr = asyncio.create_task(_stderr_tail(process.stderr))
    end = None
    error = None
    try:
        await _write_input(process.stdin, payload)
        while True:
            try:
                line = await process.stdout.readline()
                if not line:
                    break
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise ValueError("expected a typed object")
                if end is not None:
                    raise ValueError("event after end")
            except ValueError as exc:
                raise TurnError("Invalid js worker JSON stream") from exc
            if event["type"] == "approval":
                if approve is None or not isinstance(event.get("call"), dict):
                    raise TurnError("Unexpected js worker approval request")
                try:
                    refusal = await approve(event["call"])
                except Exception:  # noqa: BLE001 - a failed decision refuses the call
                    refusal = "Tool approval failed"
                if refusal is not None and not isinstance(refusal, str):
                    refusal = "Invalid tool approval decision"
                await _write_input(process.stdin, _json_line({"refusal": refusal}))
                continue
            if event["type"] == "error":
                error = event.get("message") or error
            if event["type"] == "end":
                end = event
            await emit(event)
        code = await process.wait()
        diagnostic = await asyncio.shield(stderr)
        if code or end is None or end.get("ok") is not True:
            detail = error or diagnostic or ("missing end event" if end is None else "turn failed")
            raise TurnError(f"js worker exited with status {code}: {detail}")
    finally:
        if process.returncode is None or not stderr.done():
            cleanup = asyncio.create_task(_stop(process, stderr))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
        process.stdin.close()


def _json_line(value: dict) -> bytes:
    return (json.dumps(value) + "\n").encode()


def _worker() -> int:
    from . import cli, headless, prompt_run

    with os.fdopen(os.dup(0), encoding="utf-8") as decisions, open(os.devnull, "rb") as null:
        os.dup2(null.fileno(), 0)
        request = json.loads(decisions.readline())
        sys.stdin = io.StringIO(request["prompt"])

        lock = threading.Lock()

        def request_approval(call: dict) -> str | None:
            with lock:
                stream = headless.active
                if stream is None:
                    return "Tool approval stream unavailable"
                stream.emit("approval", call=call)
                line = decisions.readline()
                if not line:
                    return "Tool approval connection closed"
                response = json.loads(line)
                return response["refusal"]

        async def approve(call: dict) -> str | None:
            return await asyncio.to_thread(request_approval, call)

        client = prompt_run.PromptClient(request["mcp_servers"], approve if request["approval"] else None)
        token = prompt_run.prompt_client.set(client)
        try:
            return cli.main()
        finally:
            prompt_run.prompt_client.reset(token)
            _kill_descendants({pid for pid, _ in descendants(os.getpid())})


if __name__ == "__main__":
    sys.exit(_worker())
