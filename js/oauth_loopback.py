"""Loopback redirect listener for browser OAuth flows.

An authorization-code flow sends the browser back to a URL on this machine
carrying ``code`` and ``state``. This binds that URL, waits for the hit, and
returns the code. It is provider-agnostic: the port, path, and page copy all
come from the caller, because each provider registers its own redirect URI and
the string has to match what the provider has on file.
"""

from __future__ import annotations

import selectors
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# How long one connection may sit idle before its handler gives up. Each
# connection has its own thread, so an idle browser preconnect stalls only
# itself, never the real callback.
HANDLER_TIMEOUT_S = 5.0


class OAuthCallbackError(RuntimeError):
    """The redirect never arrived, or arrived carrying an error."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    expected_state: str = ""
    callback_path: str = "/"
    success_html: str = ""
    failure_html: str = ""
    received_code: str | None = None
    received_state: str | None = None
    received_error: str | None = None
    # Shared by every bound server of one wait; set once a hit carrying the
    # expected state has been recorded.
    done: threading.Event
    lock: threading.Lock


class _ServerV6(_Server):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    timeout = HANDLER_TIMEOUT_S

    def log_message(self, _format: str, *_args: Any) -> None:  # noqa: A003 - stdlib API
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.server.callback_path:
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        state = (query.get("state") or [None])[0]
        code = (query.get("code") or [None])[0]
        error = (query.get("error") or [None])[0]
        # Only a hit carrying our state counts: any local process or web page
        # can reach this port, and a stray hit must not end the login.
        matched = state == self.server.expected_state and bool(code or error)
        if matched:
            with self.server.lock:
                if not self.server.done.is_set():
                    self.server.received_state = state
                    self.server.received_code = code
                    self.server.received_error = error
                    self.server.done.set()
        ok = matched and bool(code) and not error
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((self.server.success_html if ok else self.server.failure_html).encode("utf-8"))


def _bind(port: int) -> list[_Server]:
    """Bind the callback on every loopback family this host offers.

    A redirect written against ``localhost`` can resolve to ``::1`` first on a
    dual-stack box, and a browser that does not fall back to 127.0.0.1 would
    wait on a port nothing is listening on. Binding both removes that race.
    """
    servers: list[_Server] = []
    for cls, address in ((_Server, "127.0.0.1"), (_ServerV6, "::1")):
        try:
            servers.append(cls((address, port), _Handler))
        except OSError:
            continue  # this family is unavailable here; the other may still bind
    return servers


def wait_for_code(
    *,
    port: int,
    path: str,
    expected_state: str,
    timeout_s: float,
    label: str,
    success_html: str,
    failure_html: str,
) -> str:
    """Serve the redirect URI until the browser hits it; return the auth code."""
    servers = _bind(port)
    if not servers:
        raise OAuthCallbackError(f"{label} could not bind the callback port {port} on 127.0.0.1 or ::1")
    done = threading.Event()
    lock = threading.Lock()
    for server in servers:
        server.expected_state = expected_state
        server.callback_path = path
        server.success_html = success_html
        server.failure_html = failure_html
        server.timeout = 1.0
        server.done = done
        server.lock = lock

    deadline = time.monotonic() + timeout_s
    sel = selectors.DefaultSelector()
    for server in servers:
        sel.register(server, selectors.EVENT_READ, server)
    winner: _Server | None = None
    try:
        while winner is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Accepting hands the connection to its own thread and returns at
            # once; the hit is seen through `done`, set by that thread.
            for key, _events in sel.select(timeout=min(remaining, 0.2)):
                key.data.handle_request()
            if done.wait(timeout=0):
                winner = next(s for s in servers if s.received_code or s.received_error)
    finally:
        sel.close()
        for server in servers:
            server.server_close()

    if winner is None:
        raise OAuthCallbackError(f"{label} timed out waiting for the browser callback")
    if winner.received_error:
        raise OAuthCallbackError(f"{label} failed: {winner.received_error}")
    if winner.received_state != expected_state:
        raise OAuthCallbackError(f"{label} state mismatch")
    if not winner.received_code:
        raise OAuthCallbackError(f"{label} callback carried no authorization code")
    return winner.received_code
