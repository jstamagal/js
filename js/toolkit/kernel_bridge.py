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

One request is served at a time, in the order they arrive.
"""

from __future__ import annotations

import inspect
import itertools
import json
import secrets
import socket
import threading
from pathlib import Path
from typing import Any

from .core import ToolContext, ToolResult, call_scope, call_tool, registry_scope

# How long the server waits for one request's bytes after a connection opens.
# The client sends its whole request before it waits, so this bounds only a
# client that died mid-send.
REQUEST_TIMEOUT = 30.0
# How often the accept loop wakes to see whether the bridge was closed.
ACCEPT_SLICE = 0.25

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
    """

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.token = secrets.token_hex(16)
        self.registry: Any = None
        self.context: ToolContext | None = None
        self._calls = itertools.count(1)
        self._closed = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(socket_path))
        self._server.listen(8)
        self._server.settimeout(ACCEPT_SLICE)
        self._thread = threading.Thread(target=self._serve, name="js-kernel-tools", daemon=True)
        self._thread.start()

    def attach(self, registry: Any, context: ToolContext) -> None:
        self.registry = registry
        self.context = context

    def close(self) -> None:
        self._closed.set()
        try:
            self._server.close()
        except OSError:
            pass
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                self._answer(conn)

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
        except (UnicodeDecodeError, ValueError):
            request = None
        reply = self.handle(request)
        try:
            conn.settimeout(None)
            conn.sendall(json.dumps(reply, ensure_ascii=False, default=str).encode("utf-8"))
        except OSError:
            # The cell was interrupted while the tool ran; nobody is listening.
            pass

    def handle(self, request: Any) -> dict[str, Any]:
        """The reply to one decoded request: {"value": ...} or {"error": text}."""
        if not isinstance(request, dict) or not secrets.compare_digest(
                str(request.get("token", "")), self.token):
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
        try:
            with call_scope(f"kernel-tools-{next(self._calls)}"), registry_scope(registry):
                result = call_tool(tool, arguments, context)
        except Exception as exc:  # noqa: BLE001 - a failing handler is the cell's exception
            return {"error": f"ERROR running {tool.name}: {type(exc).__name__}: {exc}"}
        if isinstance(result, ToolResult) and result.is_error:
            return {"error": result.dehydrated()}
        value = _plain(result)
        if isinstance(value, str) and value.startswith("ERROR"):
            return {"error": value}
        return {"value": value}
