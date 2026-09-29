"""The `tools` object a kernel cell calls js tools through.

This file runs inside the kernel process. kernel.py sends its source to the
kernel as a cell, which builds the `js_tools` module from it and binds
`tools = js_tools.Tools(socket_path, token)` in the cell namespace. It imports
only the standard library: the kernel under `js -C` runs in the jail, where
the js package is not importable.

Each call is one connection to the unix socket kernel_bridge.py serves in the
js process: the request is one JSON document, the connection is half-closed,
and the reply is one JSON document. js runs the tool and answers.
"""

from __future__ import annotations

import json
import socket


class ToolError(Exception):
    """The js tool refused the call or reported a failure. The message is the
    ERROR text the same call would have returned to the model."""


class Tools:
    """`tools.<name>(...)` calls the js tool `<name>` and returns its result.

    Keyword arguments are the tool's parameters. Positional arguments fill the
    tool's parameters in the order its schema lists them, so
    `tools.read("setup.py")` is `tools.read(file_path="setup.py")`.
    `tools.call(name, ...)` does the same for a name that is not an identifier.
    """

    ToolError = ToolError

    def __init__(self, socket_path: str, token: str) -> None:
        self._socket_path = socket_path
        self._token = token

    def call(self, name: str, /, *args, **kwargs):
        reply = self._request({"op": "call", "tool": name, "args": list(args), "kwargs": kwargs})
        if reply.get("error") is not None:
            raise ToolError(reply["error"])
        return reply.get("value")

    def names(self) -> list[str]:
        """The tools a cell can call now."""
        reply = self._request({"op": "names"})
        if reply.get("error") is not None:
            raise ToolError(reply["error"])
        return list(reply.get("value") or [])

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            return self.call(name, *args, **kwargs)

        call.__name__ = call.__qualname__ = name
        return call

    def __dir__(self) -> list[str]:
        try:
            names = self.names()
        except (OSError, ToolError, ValueError):
            names = []
        return sorted({*names, "call", "names", "ToolError"})

    def __repr__(self) -> str:
        return "<js tools: tools.<name>(...) calls a js tool; tools.names() lists them>"

    def _request(self, payload: dict) -> dict:
        payload["token"] = self._token
        data = json.dumps(payload, default=str).encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(self._socket_path)
            conn.sendall(data)
            conn.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
        if not chunks:
            raise ToolError("ERROR: js closed the tool bridge without an answer")
        reply = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(reply, dict):
            raise ToolError("ERROR: the js tool bridge sent a malformed reply")
        return reply
