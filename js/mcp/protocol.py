"""Bidirectional, transport-independent JSON-RPC 2.0 peer for MCP."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from .types import JSONRPCId

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
REQUEST_CANCELLED = -32800

logger = logging.getLogger(__name__)


class Transport(Protocol):
    """One message-oriented MCP connection.  ``receive`` returns None at EOF."""

    async def send(self, message: dict[str, Any]) -> None: ...

    async def receive(self) -> dict[str, Any] | None: ...

    async def close(self) -> None: ...


type TransportFactory = Callable[[], Transport | Awaitable[Transport]]
type RequestHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]
type NotificationHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class JSONRPCError(Exception):
    """A remote JSON-RPC error, preserving its numeric code and optional data."""

    def __init__(self, code: int, message: str, data: Any = None, *, has_data: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.has_data = has_data

    def to_wire(self) -> dict[str, Any]:
        value = {"code": self.code, "message": self.message}
        if self.has_data or self.data is not None:
            value["data"] = self.data
        return value


class PeerClosedError(ConnectionError):
    pass


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class JSONRPCPeer:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self._ids = itertools.count(1)
        self._pending: dict[JSONRPCId, asyncio.Future[Any]] = {}
        self._incoming: dict[JSONRPCId, asyncio.Task[Any]] = {}
        self._request_handlers: dict[str, RequestHandler] = {}
        self._notification_handlers: dict[str, list[NotificationHandler]] = {}
        self._notification_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self._notification_task: asyncio.Task[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    @classmethod
    async def connect(cls, factory: TransportFactory) -> JSONRPCPeer:
        transport = await _resolve(factory())
        peer = cls(transport)
        peer.start()
        return peer

    def start(self) -> None:
        if self._closed:
            raise PeerClosedError("JSON-RPC peer is closed")
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop(), name="mcp-jsonrpc-reader")

    def add_request_handler(self, method: str, handler: RequestHandler) -> None:
        self._request_handlers[method] = handler

    def on_request(self, method: str, handler: RequestHandler) -> None:
        self.add_request_handler(method, handler)

    def add_notification_handler(self, method: str, handler: NotificationHandler) -> None:
        self._notification_handlers.setdefault(method, []).append(handler)

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self.add_notification_handler(method, handler)

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        self._ensure_running()
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = dict(params)
        try:
            await self._send(message)
            if timeout is None:
                return await asyncio.shield(future)
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except (TimeoutError, asyncio.CancelledError):
            if not future.done() and not self._closed:
                with contextlib.suppress(Exception):
                    await self._send_cancelled(request_id)
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self._ensure_running()
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._send(message)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._fail_pending(PeerClosedError("JSON-RPC peer closed"))
            current = asyncio.current_task()
            background = [task for task in self._incoming.values() if task is not current]
            notification = self._notification_task
            if notification is not None and notification is not current:
                background.append(notification)
            for task in background:
                task.cancel()
            if background:
                await asyncio.gather(*background, return_exceptions=True)
            self._incoming.clear()
            reader = self._reader_task
            if reader is not None and reader is not asyncio.current_task():
                reader.cancel()
            try:
                await self.transport.close()
            finally:
                if reader is not None and reader is not asyncio.current_task():
                    with contextlib.suppress(asyncio.CancelledError):
                        await reader

    async def __aenter__(self) -> JSONRPCPeer:
        self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    def _ensure_running(self) -> None:
        if self._closed:
            raise PeerClosedError("JSON-RPC peer is closed")
        self.start()

    async def _send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise PeerClosedError("JSON-RPC peer is closed")
        async with self._write_lock:
            if self._closed:
                raise PeerClosedError("JSON-RPC peer is closed")
            await self.transport.send(message)

    async def _send_cancelled(self, request_id: JSONRPCId) -> None:
        await self.notify(
            "notifications/cancelled",
            {"requestId": request_id, "reason": "Request cancelled by client"},
        )

    async def _read_loop(self) -> None:
        failure: BaseException = PeerClosedError("transport reached EOF")
        try:
            while True:
                message = await self.transport.receive()
                if message is None:
                    break
                await self._dispatch(message)
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            failure = PeerClosedError(f"transport failed: {exc}")
            failure.__cause__ = exc
        if not self._closed:
            self._closed = True
            self._fail_pending(failure)
            background = list(self._incoming.values())
            if self._notification_task is not None:
                background.append(self._notification_task)
            for task in background:
                task.cancel()
            if background:
                await asyncio.gather(*background, return_exceptions=True)
            self._incoming.clear()
            with contextlib.suppress(Exception):
                await self.transport.close()

    async def _dispatch(self, message: object) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return
        if "method" in message:
            method = message.get("method")
            if not isinstance(method, str):
                return
            params = message.get("params", {})
            if not isinstance(params, dict):
                if "id" in message:
                    await self._send_error(message["id"], INVALID_PARAMS, "Invalid params")
                return
            if "id" in message:
                request_id = message["id"]
                if isinstance(request_id, (int, str)) and not isinstance(request_id, bool):
                    self._start_incoming_request(request_id, method, params)
            else:
                self._dispatch_notification(method, params)
            return
        if "id" in message:
            self._dispatch_response(message)

    def _dispatch_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            text = error.get("message")
            if isinstance(code, int) and isinstance(text, str):
                future.set_exception(
                    JSONRPCError(
                        code,
                        text,
                        error.get("data"),
                        has_data="data" in error,
                    )
                )
                return
            future.set_exception(JSONRPCError(INVALID_REQUEST, "Invalid error response"))
            return
        if "result" in message:
            future.set_result(message["result"])
        else:
            future.set_exception(JSONRPCError(INVALID_REQUEST, "Response has no result or error"))

    def _start_incoming_request(
        self, request_id: JSONRPCId, method: str, params: dict[str, Any]
    ) -> None:
        task = asyncio.create_task(
            self._handle_request(request_id, method, params),
            name=f"mcp-jsonrpc-request-{request_id}",
        )
        self._incoming[request_id] = task
        task.add_done_callback(lambda _task, key=request_id: self._incoming.pop(key, None))

    async def _handle_request(
        self, request_id: JSONRPCId, method: str, params: dict[str, Any]
    ) -> None:
        handler = self._request_handlers.get(method)
        if handler is None:
            await self._send_error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")
            return
        try:
            result = await _resolve(handler(params))
        except asyncio.CancelledError:
            if not self._closed:
                await self._send_error(request_id, REQUEST_CANCELLED, "Request cancelled")
            return
        except JSONRPCError as exc:
            if not self._closed:
                await self._send({"jsonrpc": "2.0", "id": request_id, "error": exc.to_wire()})
            return
        except Exception as exc:  # noqa: BLE001 - handler failures become JSON-RPC errors
            if not self._closed:
                await self._send_error(request_id, INTERNAL_ERROR, str(exc) or "Internal error")
            return
        if not self._closed:
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _dispatch_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/cancelled":
            request_id = params.get("requestId")
            task = self._incoming.get(request_id)
            if task is not None:
                task.cancel()
        self._notification_queue.put_nowait((method, params))
        if self._notification_task is None:
            self._notification_task = asyncio.create_task(
                self._notification_loop(), name="mcp-jsonrpc-notifications"
            )

    async def _notification_loop(self) -> None:
        current = asyncio.current_task()
        try:
            while not self._closed:
                method, params = await self._notification_queue.get()
                try:
                    for handler in self._notification_handlers.get(method, ()):
                        try:
                            await _resolve(handler(params))
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001 - callbacks cannot kill the peer
                            logger.exception("MCP notification handler failed for %s", method)
                finally:
                    self._notification_queue.task_done()
        finally:
            while not self._notification_queue.empty():
                self._notification_queue.get_nowait()
                self._notification_queue.task_done()
            if self._notification_task is current:
                self._notification_task = None

    async def _send_error(self, request_id: Any, code: int, message: str) -> None:
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    def _fail_pending(self, error: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)


JsonRpcPeer = JSONRPCPeer
JsonRpcError = JSONRPCError
