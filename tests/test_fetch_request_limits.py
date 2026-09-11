"""Offline regressions for fetch's unsaved-response limits."""

from __future__ import annotations

import contextlib
import http.server
import threading
import time
from collections.abc import Iterator

import pytest

from js.toolkit import ToolContext
from js.toolkit import process_net


class _LimitHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
        if self.path == "/large.txt":
            size = process_net._DOWNLOAD_MAX_BYTES + 1
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            chunk = b"x" * (1024 * 1024)
            remaining = size
            while remaining:
                part = chunk[:remaining]
                self.wfile.write(part)
                remaining -= len(part)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        for _ in range(20):
            self.wfile.write(b"x")
            self.wfile.flush()
            time.sleep(0.1)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@contextlib.contextmanager
def _server() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LimitHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(autouse=True)
def _use_urllib_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_net, "resolve_binary", lambda _name: None)


def test_large_unsaved_text_names_inline_ceiling_and_save_workaround(tmp_path) -> None:
    with _server() as base_url:
        actual = process_net.fetch(
            f"{base_url}/large.txt",
            context=ToolContext(cwd=tmp_path, fetch_timeout_s=10),
        )

    assert actual == (
        f"ERROR: response exceeds {process_net._DOWNLOAD_MAX_BYTES} byte inline read limit; "
        "use save= to stream the full response to disk"
    )


def test_drip_feed_is_bounded_by_the_whole_request_budget(tmp_path) -> None:
    with _server() as base_url:
        started = time.monotonic()
        actual = process_net.fetch(
            f"{base_url}/drip",
            context=ToolContext(cwd=tmp_path, fetch_timeout_s=1),
        )
        elapsed = time.monotonic() - started

    assert actual == "ERROR: fetch timed out after 1 seconds"
    assert elapsed < 1.5