"""Offline browse regression against a local delayed-JavaScript page."""

from __future__ import annotations

import contextlib
import http.server
import threading
from collections.abc import Iterator

import pytest

from js.toolkit import ToolContext
from js.toolkit import search


class _DelayedHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
        body = b"""<!doctype html>
<p>SHELL-MARKER</p>
<script>
setTimeout(() => { document.body.innerHTML = '<p>DELAYED-CONTENT</p>'; }, 4000);
</script>
"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@contextlib.contextmanager
def _server() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DelayedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/delayed.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_browse_waits_for_delayed_javascript(tmp_path) -> None:
    if search.resolve_binary("obscura") is None:
        pytest.skip("managed obscura binary is not installed")

    with _server() as url:
        actual = search.browse(
            url,
            context=ToolContext(cwd=tmp_path, browse_timeout_s=15),
        )

    assert not actual.startswith("ERROR:"), actual
    assert "DELAYED-CONTENT" in actual
    assert "SHELL-MARKER" not in actual