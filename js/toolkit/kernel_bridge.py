"""The js side of the kernel's tool bridge: `tools.read(...)` in a cell.

The kernel is a separate process, so a cell reaches js tools over a unix
socket in the kernel session's private socket directory, the same directory
that holds the kernel's own sockets and that the `js -C` jail binds into the
kernel. kernel_client.py is the other end; it runs in the kernel.

A cell's call goes through the same steps as a call the model makes: the
registry the kernel call was dispatched through resolves the name (so a cell
reaches exactly the tools the agent's policy allows), the arguments are
checked against the tool's schema and the tools.yaml argument bans, and the
handler runs in this process with the live ToolContext. Path confinement
under `js -C`, read-before-write, and read coverage therefore apply unchanged:
a file a cell reads counts as read for a later `patch`, and a cell cannot
write a file nobody read.

The cell gets the handler's whole result. The per-result and per-turn caps
exist to protect the model's context, and a cell's result reaches the model
only through what the cell prints, which the kernel tool caps.

The `on` table of the turn that runs the kernel call (ToolContext.tool_call_hooks)
sees each call as a direct call: its tool_call handlers can refuse it, and
tool_result fires with the result. The observer the kernel call was dispatched
with (runtime._cell_call_observer) traces each call and logs it to the flight
log, as a direct call is.

One request is served at a time, in the order they arrive.
"""

from __future__ import annotations

import inspect
import itertools
import json
import secrets
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import jail as _jail
from .core import Output, ToolContext, ToolResult, call_scope, call_tool, registry_scope

# How long the server waits for one request's bytes after a connection opens.
# The client sends its whole request before it waits, so this bounds only a
# client that died mid-send.
REQUEST_TIMEOUT = 30.0
# How often the accept loop wakes to see whether the bridge was closed.
ACCEPT_SLICE = 0.25

# Told about every call a cell makes: (tool name, arguments, result, seconds,
# failure text or None), and refused=True for a call an `on tool_call` handler
# refused, whose result is the refusal. The runtime passes one that traces and
# logs the call.
Observer = Callable[..., None]

# Tools a cell cannot call, with the reason the refusal gives.
_OWN_KERNEL = "it runs cells in this same kernel, which is busy running the calling cell"
_REFUSED_TOOLS = {"kernel": _OWN_KERNEL, "toolbox": _OWN_KERNEL}


def client_source() -> str:
    """The source of the module the kernel builds `tools` from."""
    return (Path(__file__).with_name("kernel_client.py")).read_text(encoding="utf-8")


def install_cell(socket_path: Path, token: str) -> str:
    """The cell that binds `tools` in the kernel's namespace."""
    return (
        "def __js_install_tools():\n"
        "    import sys, types\n"
        "    module = types.ModuleType('js_tools')\n"
        f"    exec(compile({client_source()!r}, 'js_tools', 'exec'), module.__dict__)\n"
        "    sys.modules['js_tools'] = module\n"
        f"    globals()['tools'] = module.Tools({str(socket_path)!r}, {token!r})\n"
        "__js_install_tools()\n"
        "del __js_install_tools\n"
    )


def _refusal(tool: Any) -> str | None:
    """Why ``tool`` cannot run from a cell, or None when it can."""
    from . import meta

    reason = _REFUSED_TOOLS.get(tool.name)
    if reason is None and meta.is_fan_out_handler(tool.handler):
        reason = "it runs subagent turns; call it directly"
    if reason is None and inspect.iscoroutinefunction(tool.handler):
        reason = "it runs on js's event loop; call it directly"
    if reason is None:
        return None
    return f"ERROR: {tool.name} cannot be called from a kernel cell: {reason}."


def _parameter_names(tool: Any) -> list[str]:
    schema = tool.openai_spec()["function"]["parameters"]
    properties = schema.get("properties") if isinstance(schema, dict) else None
    return list(properties) if isinstance(properties, dict) else []


def _encode(reply: dict[str, Any]) -> bytes:
    """``reply`` as the bytes of one JSON document. ASCII escapes carry any
    str, including the lone surrogates a non-UTF-8 file name decodes to."""
    try:
        return json.dumps(reply, default=str).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        return json.dumps({"error": f"ERROR: the js tool bridge could not send the result: "
                                    f"{type(exc).__name__}: {exc}"}).encode("ascii")


def _plain(value: Any) -> Any:
    """``value`` as something JSON carries: text for a mixed result, the
    string form of anything JSON has no type for."""
    if isinstance(value, ToolResult):
        return value.dehydrated()
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


class ToolBridge:
    """A unix socket that runs the tool calls a kernel's cells send.

    `attach` names the registry and ToolContext calls run with; the kernel
    tool attaches on every call, so a cell uses the surface of the agent whose
    call is running it. Until something attaches, every call is refused.

    Every connection gets a reply, whatever the request held or the tool
    returned, and nothing a request does stops the serving thread. `revive`
    serves again on the same path with the same token if the thread stopped
    anyway, so the `tools` object already in the kernel keeps working.
    """

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.token = secrets.token_hex(16)
        self.registry: Any = None
        self.context: ToolContext | None = None
        self.observer: Observer | None = None
        self._calls = itertools.count(1)
        self._closed = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._listen()

    def _listen(self) -> None:
        self.socket_path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        server.listen(8)
        server.settimeout(ACCEPT_SLICE)
        self._server = server
        self._thread = threading.Thread(target=self._serve, args=(server,),
                                        name="js-kernel-tools", daemon=True)
        self._thread.start()

    def serving(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def revive(self) -> None:
        """Serve again if the serving thread has stopped and the bridge is open."""
        if self._closed.is_set() or self.serving():
            return
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        self._listen()

    def attach(self, registry: Any, context: ToolContext,
               observer: Observer | None = None) -> None:
        self.registry = registry
        self.context = context
        self.observer = observer

    def close(self) -> None:
        self._closed.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _serve(self, server: socket.socket) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                with conn:
                    self._answer(conn)
            except Exception:  # noqa: BLE001 - one bad connection must not stop the bridge
                continue

    def _answer(self, conn: socket.socket) -> None:
        conn.settimeout(REQUEST_TIMEOUT)
        chunks: list[bytes] = []
        try:
            while True:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError:
            return
        try:
            request = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            request = None
        try:
            reply = self.handle(request)
        except Exception as exc:  # noqa: BLE001 - the cell gets the failure as ToolError
            reply = {"error": f"ERROR: the js tool bridge failed: {type(exc).__name__}: {exc}"}
        try:
            conn.settimeout(None)
            conn.sendall(_encode(reply))
        except OSError:
            # The cell was interrupted while the tool ran; nobody is listening.
            pass

    def handle(self, request: Any) -> dict[str, Any]:
        """The reply to one decoded request: {"value": ...} or {"error": text}."""
        token = request.get("token") if isinstance(request, dict) else None
        if not (isinstance(token, str) and token.isascii()
                and secrets.compare_digest(token, self.token)):
            return {"error": "ERROR: the js tool bridge refused a request it could not authenticate"}
        registry, context = self.registry, self.context
        if registry is None or context is None:
            return {"error": "ERROR: no js tool call is attached to this kernel, "
                             "so its cells cannot call tools"}
        op = request.get("op")
        if op == "names":
            return {"value": sorted(tool.name for tool in registry.tools if _refusal(tool) is None)}
        if op != "call":
            return {"error": f"ERROR: unknown tool bridge request {op!r}"}
        return self._call(registry, context, request)

    def _call(self, registry: Any, context: ToolContext, request: dict) -> dict[str, Any]:
        from .. import tool_args

        name = str(request.get("tool", ""))
        tool = registry.resolve(name)
        if tool is None:
            return {"error": registry.unavailable_error(name)}
        refusal = _refusal(tool)
        if refusal is not None:
            return {"error": refusal}
        positional = request.get("args") or []
        arguments = request.get("kwargs") or {}
        if not isinstance(positional, list) or not isinstance(arguments, dict):
            return {"error": f"ERROR: invalid arguments for {tool.name}"}
        names = _parameter_names(tool)
        if len(positional) > len(names):
            return {"error": f"ERROR: {tool.name} takes at most {len(names)} positional "
                             f"arguments ({', '.join(names)}); {len(positional)} given"}
        for key, value in zip(names, positional):
            if key in arguments:
                return {"error": f"ERROR: {tool.name} got {key!r} both by position and by name"}
            arguments[key] = value
        schema = tool.openai_spec()["function"]["parameters"]
        arguments = tool_args.coerce_json_containers(arguments, schema)
        problem = tool_args.schema_error(arguments, schema)
        if problem is not None:
            return {"error": f"ERROR: invalid arguments for {tool.name}: {problem}"}
        banned = registry.argument_refusal(tool.name, arguments)
        if banned is not None:
            return {"error": banned}
        observer = self.observer
        call_id = f"kernel-tools-{next(self._calls)}"
        hooks = getattr(context, "tool_call_hooks", None)
        refusal = _hook_refusal(hooks, call_id, tool.name, arguments)
        if refusal is not None:
            if observer is not None:
                observer(tool.name, arguments, refusal, 0.0, None, refused=True)
            return {"error": refusal}
        started = time.monotonic()
        try:
            with call_scope(call_id), registry_scope(registry, observer):
                result = call_tool(tool, arguments, context)
        except Exception as exc:  # noqa: BLE001 - a failing handler is the cell's exception
            failure = f"{type(exc).__name__}: {exc}"
            result = _jail.shown(f"ERROR running {tool.name}: {failure}")
            if observer is not None:
                observer(tool.name, arguments, result, time.monotonic() - started, failure)
            _emit(hooks, "tool_result", id=call_id, name=tool.name, result=result)
            return {"error": result}
        if observer is not None:
            observer(tool.name, arguments, result, time.monotonic() - started, None)
        _emit(hooks, "tool_result", id=call_id, name=tool.name, result=result)
        if isinstance(result, ToolResult) and result.is_error:
            return {"error": result.dehydrated()}
        value = _plain(result)
        if isinstance(value, str) and value.startswith("ERROR") and not isinstance(result, Output):
            return {"error": value}
        return {"value": value}


def _emit(hooks: Any, event: str, **payload: Any) -> Any:
    """``event`` raised to ``hooks``, the `on` table of the turn whose kernel
    call runs the cell; None when there is none or its handlers failed to run."""
    if hooks is None:
        return None
    try:
        return hooks.emit(event, **payload)
    except Exception:  # noqa: BLE001 - a handler failure never breaks the call
        return None


def _hook_refusal(hooks: Any, call_id: str, name: str, arguments: dict) -> str | None:
    """The refusal an `on tool_call` handler gives this call, or None. The
    payload has the shape a direct call's has: arguments as JSON text."""
    from .. import events

    emission = _emit(hooks, "tool_call", id=call_id, name=name,
                     arguments=json.dumps(arguments, default=str))
    return events.refusal_of(emission)
