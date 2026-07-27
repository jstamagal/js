"""Concrete transports for Model Context Protocol connections."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.client
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .types import LATEST_PROTOCOL_VERSION

Redactor = Callable[[str], str]


class StreamableHTTPTransportError(ConnectionError):
    """A safe-to-display streamable HTTP transport failure."""


class _TrackedConnectionMixin:
    """Registers the raw TCP socket the instant it is created — before any
    proxy tunnel, TLS handshake, or header wait — so close() can always
    reach a stalled operation."""

    def __init__(self, *args: Any, track: Callable[[Any], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        original = self._create_connection

        def create_and_track(*create_args: Any, **create_kwargs: Any) -> Any:
            sock = original(*create_args, **create_kwargs)
            track(sock)
            return sock

        self._create_connection = create_and_track


class _TrackedHTTPConnection(_TrackedConnectionMixin, http.client.HTTPConnection):
    pass


class _TrackedHTTPSConnection(_TrackedConnectionMixin, http.client.HTTPSConnection):
    pass


class _SocketTrackingHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, track: Callable[[Any], None]) -> None:
        super().__init__()
        self._track = track

    def http_open(self, req: urllib.request.Request):
        return self.do_open(functools.partial(_TrackedHTTPConnection, track=self._track), req)


class _SocketTrackingHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, track: Callable[[Any], None]) -> None:
        super().__init__()
        self._track = track

    def https_open(self, req: urllib.request.Request):
        return self.do_open(
            functools.partial(_TrackedHTTPSConnection, track=self._track), req, context=self._context
        )


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


class StreamableHTTPTransport:
    """Current MCP streamable HTTP transport using only the standard library."""

    _ACCEPT = "application/json, text/event-stream"

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        protocol_version: str = LATEST_PROTOCOL_VERSION,
        name: str = "HTTP server",
        redactor: Redactor | None = None,
        timeout: float = 10.0,
        max_response_bytes: int = 8 * 1024 * 1024,
        reconnect_attempts: int = 3,
        reconnect_backoff: float = 0.05,
    ) -> None:
        if not isinstance(url, str) or not url:
            raise ValueError("MCP HTTP URL must be a non-empty string")
        self._url = url
        self._configured_headers = {str(key): str(value) for key, value in (headers or {}).items()}
        self._protocol_version = str(protocol_version)
        self._name = str(name)
        self._redactor = redactor
        self._timeout = max(0.05, timeout)
        self._max_response_bytes = max(1024, max_response_bytes)
        self._reconnect_attempts = max(0, reconnect_attempts)
        self._reconnect_backoff = max(0.0, reconnect_backoff)
        self._session_id: str | None = None
        self._last_event_id: str | None = None
        self._retry_seconds = self._reconnect_backoff
        self._queue: asyncio.Queue[dict[str, Any] | BaseException | None] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._get_task: asyncio.Task[None] | None = None
        self._post_tasks: set[asyncio.Task[None]] = set()
        self._request_tasks: set[asyncio.Task[tuple[list[dict[str, Any]], Any | None]]] = set()
        self._get_supported = True
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active: set[Any] = set()
        self._active_lock = threading.Lock()
        self._closing = threading.Event()
        self._closed = False
        self._pending_socks: dict[int, list[socket.socket]] = {}
        self._opener = urllib.request.build_opener(
            _SocketTrackingHTTPHandler(self._track_socket),
            _SocketTrackingHTTPSHandler(self._track_socket),
        )

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def last_event_id(self) -> str | None:
        return self._last_event_id

    async def start(self) -> StreamableHTTPTransport:
        if self._closed:
            raise StreamableHTTPTransportError(self._diagnostic("is closed"))
        self._loop = asyncio.get_running_loop()
        return self

    async def send(self, message: dict[str, Any]) -> None:
        await self.start()
        try:
            payload = json.dumps(
                message, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise StreamableHTTPTransportError(
                self._diagnostic("was given an invalid JSON message")
            ) from None
        is_request = "method" in message and "id" in message
        async with self._send_lock:
            request_task = asyncio.create_task(
                asyncio.to_thread(self._post, payload, is_request), name="mcp-http-post"
            )
            self._request_tasks.add(request_task)
            try:
                messages, stream = await asyncio.shield(request_task)
            except asyncio.CancelledError:
                self._close_active_responses()
                with contextlib.suppress(StreamableHTTPTransportError):
                    _messages, stream = await asyncio.shield(request_task)
                    if stream is not None:
                        self._release(stream)
                raise
            finally:
                self._request_tasks.discard(request_task)
            if self._closed:
                if stream is not None:
                    self._release(stream)
                raise StreamableHTTPTransportError(self._diagnostic("is closed"))
            for incoming in messages:
                self._queue.put_nowait(incoming)
            if stream is not None:
                task = asyncio.create_task(
                    self._consume_post_stream(stream), name="mcp-http-post-response"
                )
                self._post_tasks.add(task)
                task.add_done_callback(self._post_tasks.discard)
            self._ensure_get_stream()

    async def receive(self) -> dict[str, Any] | None:
        await self.start()
        item = await self._queue.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._closing.set()
            self._close_active_responses()
            get_task, self._get_task = self._get_task, None
            background = list(self._post_tasks)
            self._post_tasks.clear()
            if get_task is not None:
                background.append(get_task)
            for task in background:
                task.cancel()
            if background:
                await asyncio.gather(*background, return_exceptions=True)
            requests = list(self._request_tasks)
            if requests:
                await asyncio.gather(*requests, return_exceptions=True)
                self._close_active_responses()
            session_id = self._session_id
            if session_id is not None:
                with contextlib.suppress(StreamableHTTPTransportError):
                    await asyncio.to_thread(self._delete, session_id)
            self._queue.put_nowait(None)

    async def __aenter__(self) -> StreamableHTTPTransport:
        return await self.start()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    def _post(
        self, payload: bytes, is_request: bool
    ) -> tuple[list[dict[str, Any]], Any | None]:
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._url, data=payload, headers=headers, method="POST")
        try:
            response = self._open(request)
        except BaseException:
            self._drop_thread_sockets()
            raise
        keep_stream = False
        try:
            self._capture_session(response)
            status = response.status
            if not is_request:
                if status != 202 or self._bounded_read(response):
                    raise StreamableHTTPTransportError(
                        self._diagnostic("returned an invalid response to a JSON-RPC message")
                    )
                return [], None
            if status != 200:
                raise StreamableHTTPTransportError(
                    self._diagnostic("returned an invalid response to a JSON-RPC request")
                )
            content_type = self._content_type(response)
            if content_type == "application/json":
                return [self._decode_message(self._bounded_read(response))], None
            if content_type == "text/event-stream":
                # A POST response stream may remain open indefinitely.  Hand it to a
                # background reader so send() returns after the response headers arrive.
                keep_stream = True
                return [], response
            raise StreamableHTTPTransportError(
                self._diagnostic("returned an unsupported Content-Type")
            )
        except StreamableHTTPTransportError:
            raise
        except Exception:  # noqa: BLE001 - wire failures must be credential-safe
            raise StreamableHTTPTransportError(self._diagnostic("POST request failed")) from None
        finally:
            self._drop_thread_sockets()
            if not keep_stream:
                self._release(response)

    def _delete(self, session_id: str) -> None:
        headers = self._headers(session_id=session_id)
        request = urllib.request.Request(self._url, headers=headers, method="DELETE")
        response = self._open(request, allow_closing=True)
        try:
            if response.status not in {200, 202, 204}:
                raise StreamableHTTPTransportError(
                    self._diagnostic(f"session shutdown failed with HTTP {response.status}")
                )
            self._bounded_read(response)
        finally:
            self._release(response)

    def _get_once(self) -> None:
        headers = self._headers()
        if self._last_event_id is not None:
            headers["Last-Event-ID"] = self._last_event_id
        request = urllib.request.Request(self._url, headers=headers, method="GET")
        try:
            response = self._open(request, allowed_errors={405})
        except BaseException:
            self._drop_thread_sockets()
            raise
        try:
            if response.status == 405:
                self._get_supported = False
                return
            self._capture_session(response)
            if self._content_type(response) != "text/event-stream":
                raise StreamableHTTPTransportError(
                    self._diagnostic("notification stream returned an unsupported Content-Type")
                )
            self._consume_sse(response, self._emit_from_thread)
        except StreamableHTTPTransportError:
            raise
        except Exception:  # noqa: BLE001 - wire failures must be credential-safe
            raise StreamableHTTPTransportError(
                self._diagnostic("notification stream failed")
            ) from None
        finally:
            self._drop_thread_sockets()
            self._release(response)

    async def _consume_post_stream(self, response: Any) -> None:
        try:
            await asyncio.to_thread(self._consume_sse, response, self._emit_from_thread)
        except asyncio.CancelledError:
            self._release(response)
            raise
        except StreamableHTTPTransportError as exc:
            if not self._closed:
                self._queue.put_nowait(exc)
        except Exception:  # noqa: BLE001 - wire failures must be credential-safe
            if not self._closed:
                self._queue.put_nowait(
                    StreamableHTTPTransportError(self._diagnostic("POST response stream failed"))
                )
        finally:
            self._release(response)

    async def _get_loop(self) -> None:
        failures = 0
        while not self._closed and self._get_supported:
            try:
                await asyncio.to_thread(self._get_once)
                failures = 0
            except asyncio.CancelledError:
                return
            except StreamableHTTPTransportError as exc:
                if self._closed:
                    return
                failures += 1
                if failures > self._reconnect_attempts:
                    self._queue.put_nowait(exc)
                    return
            if not self._closed:
                await asyncio.sleep(self._retry_seconds)

    def _ensure_get_stream(self) -> None:
        if self._closed or not self._get_supported:
            return
        if self._get_task is None or self._get_task.done():
            self._get_task = asyncio.create_task(self._get_loop(), name="mcp-http-notifications")

    def _open(
        self,
        request: urllib.request.Request,
        *,
        allow_closing: bool = False,
        allowed_errors: set[int] | None = None,
    ):
        if self._closing.is_set() and not allow_closing:
            raise StreamableHTTPTransportError(self._diagnostic("is closed"))
        try:
            if allow_closing:
                # close()'s own DELETE must not be shut down by close().
                response = urllib.request.urlopen(request, timeout=self._timeout)  # noqa: S310
            else:
                response = self._opener.open(request, timeout=self._timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            if allowed_errors is not None and exc.code in allowed_errors:
                response = exc
            else:
                with contextlib.suppress(Exception):
                    exc.close()
                raise StreamableHTTPTransportError(
                    self._diagnostic(f"request failed with HTTP {exc.code}")
                ) from None
        except Exception:  # noqa: BLE001 - urllib errors may contain request headers
            raise StreamableHTTPTransportError(self._diagnostic("request failed")) from None
        reject_response = False
        with self._active_lock:
            if self._closing.is_set() and not allow_closing:
                reject_response = True
            else:
                self._active.add(response)
        if reject_response:
            with contextlib.suppress(Exception):
                response.close()
            raise StreamableHTTPTransportError(self._diagnostic("is closed"))
        return response

    def _headers(self, *, session_id: str | None = None) -> dict[str, str]:
        headers = dict(self._configured_headers)
        headers["Accept"] = self._ACCEPT
        headers["MCP-Protocol-Version"] = self._protocol_version
        current_session = self._session_id if session_id is None else session_id
        if current_session is not None:
            headers["Mcp-Session-Id"] = current_session
        return headers

    def _capture_session(self, response: Any) -> None:
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_id = session_id

    @staticmethod
    def _content_type(response: Any) -> str:
        return response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()

    def _bounded_read(self, response: Any) -> bytes:
        data = response.read(self._max_response_bytes + 1)
        if len(data) > self._max_response_bytes:
            raise StreamableHTTPTransportError(self._diagnostic("response was too large"))
        return data

    def _consume_sse(self, response: Any, emit: Callable[[dict[str, Any]], None]) -> None:
        data_lines: list[bytes] = []
        event_size = 0
        while not self._closing.is_set():
            line = response.readline(self._max_response_bytes + 1)
            if not line:
                break
            event_size += len(line)
            if event_size > self._max_response_bytes:
                raise StreamableHTTPTransportError(self._diagnostic("SSE event was too large"))
            line = line.rstrip(b"\r\n")
            if not line:
                if data_lines:
                    emit(self._decode_message(b"\n".join(data_lines)))
                data_lines.clear()
                event_size = 0
                continue
            if line.startswith(b":"):
                continue
            field, separator, value = line.partition(b":")
            if separator and value.startswith(b" "):
                value = value[1:]
            if field == b"data":
                data_lines.append(value)
            elif field == b"id" and b"\x00" not in value:
                self._last_event_id = value.decode("utf-8", "replace")
            elif field == b"retry":
                with contextlib.suppress(ValueError):
                    retry = int(value)
                    if retry >= 0:
                        self._retry_seconds = retry / 1000
        if data_lines:
            emit(self._decode_message(b"\n".join(data_lines)))

    def _decode_message(self, payload: bytes) -> dict[str, Any]:
        try:
            message = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise StreamableHTTPTransportError(
                self._diagnostic("returned invalid protocol JSON")
            ) from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise StreamableHTTPTransportError(
                self._diagnostic("returned invalid protocol JSON")
            )
        return message

    def _emit_from_thread(self, message: dict[str, Any]) -> None:
        loop = self._loop
        if loop is not None and not self._closed:
            loop.call_soon_threadsafe(self._queue.put_nowait, message)

    def _release(self, response: Any) -> None:
        with self._active_lock:
            self._active.discard(response)
        with contextlib.suppress(Exception):
            response.close()

    def _track_socket(self, sock: Any) -> None:
        # A dup'd handle stays valid after ssl wraps and detaches the
        # original socket object, so it can interrupt any later phase.
        handle = socket.socket(fileno=os.dup(sock.fileno()))
        with self._active_lock:
            self._pending_socks.setdefault(threading.get_ident(), []).append(handle)
            closing = self._closing.is_set()
        if closing:
            with contextlib.suppress(Exception):
                handle.shutdown(2)

    def _drop_thread_sockets(self) -> None:
        with self._active_lock:
            handles = self._pending_socks.pop(threading.get_ident(), [])
        for handle in handles:
            with contextlib.suppress(Exception):
                handle.close()

    def _close_active_responses(self) -> None:
        with self._active_lock:
            responses = tuple(self._active)
            handles = [h for items in self._pending_socks.values() for h in items]
        # Interrupt connections still connecting, tunneling, in a TLS
        # handshake, or waiting for headers; only their owning thread
        # closes the handles.
        for handle in handles:
            with contextlib.suppress(Exception):
                handle.shutdown(2)
        for response in responses:
            # Closing HTTPResponse alone does not reliably wake a thread blocked in
            # socket readline().  Shut down the underlying connection first.
            with contextlib.suppress(Exception):
                response.fp.raw._sock.shutdown(2)
            with contextlib.suppress(Exception):
                response.close()

    def _diagnostic(self, detail: str) -> str:
        name = self._safe_name()
        return f"MCP streamable HTTP server {name!r} {detail}"

    def _safe_name(self) -> str:
        value = self._name
        for configured in self._configured_headers.values():
            secrets = (configured, *configured.split())
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
        if self._redactor is None:
            return value
        try:
            return self._redactor(value)
        except Exception:  # noqa: BLE001 - a broken redactor must not expose raw values
            return "[REDACTED]"
