"""Concrete transports for Model Context Protocol connections."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable, Iterable, Mapping
from typing import Any

Redactor = Callable[[str], str]


class StdioTransportError(ConnectionError):
    """A safe-to-display stdio transport failure."""


class StdioTransport:
    """Newline-delimited JSON-RPC over a directly spawned child process.

    The child inherits the current environment, with configured values overlaid.
    Its stderr is always drained but is deliberately not surfaced: MCP server
    stderr is untrusted and may contain configured credentials.
    """

    def __init__(
        self,
        command: str,
        args: Iterable[str] = (),
        *,
        env: Mapping[str, str] | None = None,
        name: str | None = None,
        redactor: Redactor | None = None,
        close_timeout: float = 1.0,
        terminate_timeout: float = 1.0,
    ) -> None:
        if not isinstance(command, str) or not command:
            raise ValueError("MCP stdio command must be a non-empty string")
        self._command = command
        self._args = tuple(str(arg) for arg in args)
        self._configured_env = {str(key): str(value) for key, value in (env or {}).items()}
        self._name = name or "stdio server"
        self._redactor = redactor
        self._close_timeout = max(0.0, close_timeout)
        self._terminate_timeout = max(0.0, terminate_timeout)
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stdout_buffer = bytearray()
        self._start_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        """The spawned process, exposed for lifecycle diagnostics and tests."""
        return self._process

    async def start(self) -> StdioTransport:
        async with self._start_lock:
            if self._closed:
                raise StdioTransportError(self._diagnostic("is closed"))
            if self._process is not None:
                return self
            child_env = os.environ.copy()
            child_env.update(self._configured_env)
            try:
                process = await asyncio.create_subprocess_exec(
                    self._command,
                    *self._args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=child_env,
                )
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError):
                raise StdioTransportError(self._diagnostic("could not be started")) from None
            self._process = process
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(process), name="mcp-stdio-stderr"
            )
            return self

    async def send(self, message: dict[str, Any]) -> None:
        await self.start()
        process = self._process
        if process is None or process.stdin is None:
            raise StdioTransportError(self._diagnostic("has no stdin"))
        if process.returncode is not None:
            raise self._death_error(process)
        try:
            payload = json.dumps(
                message, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError):
            raise StdioTransportError(self._diagnostic("was given an invalid JSON message")) from None
        async with self._write_lock:
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except asyncio.CancelledError:
                raise
            except (BrokenPipeError, ConnectionError, OSError):
                await self._refresh_returncode(process)
                raise self._death_error(process) from None

    async def receive(self) -> dict[str, Any] | None:
        await self.start()
        process = self._process
        if process is None or process.stdout is None:
            raise StdioTransportError(self._diagnostic("has no stdout"))
        try:
            async with self._read_lock:
                line = await self._readline(process.stdout)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError):
            error = StdioTransportError(self._diagnostic("stdout failed"))
            await self._abort()
            raise error from None
        if not line:
            if self._closed:
                return None
            await self._refresh_returncode(process)
            error = self._death_error(process)
            await self._abort()
            raise error
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            error = StdioTransportError(self._diagnostic("wrote non-protocol data to stdout"))
            await self._abort()
            raise error from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            error = StdioTransportError(self._diagnostic("wrote non-protocol data to stdout"))
            await self._abort()
            raise error
        return message

    async def close(self) -> None:
        # Wait for an in-flight spawn before deciding that there is no child to reap.
        async with self._start_lock:
            async with self._close_lock:
                self._closed = True
                process = self._process
                if process is None:
                    return
                if process.stdin is not None:
                    # Do not await wait_closed() before reaping the process. It waits for
                    # buffered writes to drain and can hang forever when the child does
                    # not read stdin. Process exit closes the pipe for us.
                    process.stdin.close()
                if process.returncode is None and not await self._wait(process, self._close_timeout):
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                    if not await self._wait(process, self._terminate_timeout):
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
                else:
                    await process.wait()
                stderr_task, self._stderr_task = self._stderr_task, None
                if stderr_task is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await stderr_task

    async def _abort(self) -> None:
        """Reap a broken child without waiting for stdin-driven graceful exit."""
        async with self._start_lock:
            async with self._close_lock:
                if self._closed:
                    return
                self._closed = True
                process = self._process
                if process is None:
                    return
                if process.stdin is not None:
                    process.stdin.close()
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                    if not await self._wait(process, self._terminate_timeout):
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
                else:
                    await process.wait()
                stderr_task, self._stderr_task = self._stderr_task, None
                if stderr_task is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await stderr_task

    async def __aenter__(self) -> StdioTransport:
        return await self.start()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def _readline(self, stream: asyncio.StreamReader) -> bytes:
        """Read one line without StreamReader's default 64 KiB readline limit."""
        while True:
            newline = self._stdout_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._stdout_buffer[: newline + 1])
                del self._stdout_buffer[: newline + 1]
                return line
            chunk = await stream.read(65536)
            if chunk:
                self._stdout_buffer.extend(chunk)
                continue
            line = bytes(self._stdout_buffer)
            self._stdout_buffer.clear()
            return line

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        stream = process.stderr
        if stream is None:
            return
        try:
            while await stream.read(65536):
                pass
        except (ConnectionError, OSError):
            pass

    async def _refresh_returncode(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 0.05)

    async def _wait(self, process: asyncio.subprocess.Process, timeout: float) -> bool:
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout)
        except TimeoutError:
            return False
        return True

    def _death_error(self, process: asyncio.subprocess.Process) -> StdioTransportError:
        if process.returncode is None:
            detail = "connection closed unexpectedly"
        else:
            detail = f"exited with status {process.returncode}"
        return StdioTransportError(self._diagnostic(detail))

    def _diagnostic(self, detail: str) -> str:
        server = self._redact(self._name)
        executable = self._redact(self._command)
        return f"MCP stdio server {server!r} (executable {executable!r}) {detail}"

    def _redact(self, value: str) -> str:
        if self._redactor is None:
            return value
        try:
            return self._redactor(value)
        except Exception:  # noqa: BLE001 - a broken redactor must not expose raw values
            return "[REDACTED]"
