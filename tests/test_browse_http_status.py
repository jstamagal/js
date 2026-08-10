"""browse must not hand back a 404 page as if it were the document.

obscura exits 0 on an error response, prints "Page loaded", and dumps the error
body as ordinary content. Measured against a local server: a 404 came back as
"# 404 page / This is the error body" with nothing marking it an error.
"""
from __future__ import annotations

import http.server
import shutil
import socketserver
import threading

import pytest

from js.toolkit import ToolContext
from js.toolkit import search as search_mod
from js.toolkit.search import browse


requires_obscura = pytest.mark.skipif(
    shutil.which("obscura") is None, reason="obscura binary not installed"
)


SEEN_AGENTS: list[str] = []


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        SEEN_AGENTS.append(self.headers.get("User-Agent") or "")
        code = int(self.path.strip("/") or 200)
        body = (
            f"<html><title>Server Says {code}</title><body>"
            f"<h1>{code} page</h1><p>DISTINCTIVE_ERROR_BODY</p></body></html>"
        ).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def server():
    SEEN_AGENTS.clear()
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _StatusHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@requires_obscura
@pytest.mark.parametrize("code", [404, 500, 403])
def test_an_error_response_is_reported_as_an_error(server, tmp_path, code):
    context = ToolContext(cwd=tmp_path)
    result = browse(f"{server}/{code}", context=context)

    assert result.startswith("ERROR:"), result
    assert f"HTTP {code}" in result
    # The body is kept -- an error page is sometimes what the caller wanted.
    assert "DISTINCTIVE_ERROR_BODY" in result


@requires_obscura
def test_a_normal_page_is_not_marked_as_an_error(server, tmp_path):
    context = ToolContext(cwd=tmp_path)
    result = browse(f"{server}/200", context=context)

    assert not result.startswith("ERROR:"), result
    assert "DISTINCTIVE_ERROR_BODY" in result


@requires_obscura
def test_credentials_in_the_url_do_not_reach_the_error_line(server, tmp_path):
    host = server.split("//", 1)[1]
    context = ToolContext(cwd=tmp_path)
    result = browse(f"http://admin:hunter2@{host}/404", context=context)

    assert result.startswith("ERROR:")
    assert "hunter2" not in result
    assert "admin" not in result


@requires_obscura
def test_neither_request_ever_names_js_to_the_site(server, tmp_path):
    """js must not identify itself to a site. obscura owns the identity on both
    the status probe and the render -- a js-agent probe got a different
    bot-detection verdict than the browser and stamped ERROR on good pages."""
    browse(f"{server}/200", context=ToolContext(cwd=tmp_path))

    assert len(SEEN_AGENTS) >= 2
    assert all("js-agent" not in agent for agent in SEEN_AGENTS)
    assert all("Chrome/" in agent for agent in SEEN_AGENTS)


@requires_obscura
def test_the_status_comes_from_obscura_not_a_second_client(server, tmp_path):
    status = search_mod._obscura_status(
        shutil.which("obscura"), f"{server}/503", timeout=20
    )

    assert status == 503
