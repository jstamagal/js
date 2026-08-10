from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from js import tool_binaries
from js.toolkit import ToolContext
from js.toolkit import process_net


_PAYLOAD = (b"aria2-real-transfer\x00" * (12 * 1024 * 1024 // 20 + 1))[: 12 * 1024 * 1024]
_TEXT_PAYLOAD = (b"aria2 large text response\n" * (6 * 1024 * 1024 // 26 + 1))[
    : 6 * 1024 * 1024
]


class _TransferServer(ThreadingHTTPServer):
    daemon_threads = True


class _TransferHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    @property
    def state(self) -> SimpleNamespace:
        return self.server.state  # type: ignore[attr-defined, no-any-return]

    def do_HEAD(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/missing":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        payload = _TEXT_PAYLOAD if self.path == "/text" else _PAYLOAD
        length = len(payload) + 65536 if self.path == "/truncated" else len(payload)
        content_type = "text/plain" if self.path == "/text" else "application/octet-stream"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_GET(self) -> None:
        requested_range = self.headers.get("Range", "")
        with self.state.lock:
            self.state.requests.append((self.path, requested_range))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/missing":
            body = b"missing"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        payload = _TEXT_PAYLOAD if self.path == "/text" else _PAYLOAD
        advertised_length = len(payload) + 65536 if self.path == "/truncated" else None
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested_range)
        if match is None:
            start, end, status = 0, len(payload) - 1, 200
        else:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else len(payload) - 1
            end = min(end, len(payload) - 1)
            status = 206
        body = payload[start : end + 1]
        self.send_response(status)
        content_type = "text/plain" if self.path == "/text" else "application/octet-stream"
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
        self.send_header("Content-Length", str(advertised_length or len(body)))
        self.end_headers()
        try:
            for offset in range(0, len(body), 64 * 1024):
                chunk = body[offset : offset + 64 * 1024]
                self.wfile.write(chunk)
                self.wfile.flush()
                with self.state.lock:
                    self.state.bytes_sent += len(chunk)
                if self.path == "/slow" and self.state.slow:
                    time.sleep(0.03)
        except (BrokenPipeError, ConnectionResetError):
            pass
        if self.path == "/truncated":
            self.close_connection = True


@pytest.fixture(scope="module")
def transfer_server() -> Iterator[tuple[str, SimpleNamespace]]:
    state = SimpleNamespace(requests=[], bytes_sent=0, slow=True, lock=threading.Lock())
    server = _TransferServer(("127.0.0.1", 0), _TransferHandler)
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def aria2c() -> str:
    binary = tool_binaries.resolve_binary(tool_binaries.ARIA2_EXECUTABLE)
    assert binary is not None
    return binary


def _ranges(state: SimpleNamespace, path: str) -> list[str]:
    with state.lock:
        return [value for requested_path, value in state.requests if requested_path == path and value]


def _clear_server_state(state: SimpleNamespace) -> None:
    with state.lock:
        state.requests.clear()
        state.bytes_sent = 0


def test_fetch_saved_response_uses_segmented_aria2_transfer(
    transfer_server, tmp_path: Path
) -> None:
    base_url, state = transfer_server
    _clear_server_state(state)
    target = tmp_path / "saved.bin"

    result = process_net.fetch(
        f"{base_url}/file",
        save=target.name,
        context=ToolContext(cwd=tmp_path, download_timeout_s=10),
    )

    assert target.read_bytes() == _PAYLOAD
    assert result == (
        f"SAVED_RESPONSE path={target} size={len(_PAYLOAD)} bytes "
        "content-type=application/octet-stream"
    )
    assert len(_ranges(state, "/file")) >= 2


def test_unsaved_binary_response_uses_aria2_and_keeps_descriptor_contract(
    transfer_server,
) -> None:
    base_url, state = transfer_server
    _clear_server_state(state)

    result = process_net.fetch(
        f"{base_url}/file",
        context=ToolContext(cwd=Path.cwd(), download_timeout_s=10),
    )

    assert result == (
        f"BINARY_RESPONSE content-type=application/octet-stream size={len(_PAYLOAD)} bytes "
        "[truncated]"
    )
    assert len(_ranges(state, "/file")) >= 2


def test_unsaved_large_text_uses_aria2_and_keeps_spill_contract(
    transfer_server, monkeypatch, tmp_path: Path
) -> None:
    base_url, state = transfer_server
    _clear_server_state(state)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = process_net.fetch(
        f"{base_url}/text",
        context=ToolContext(
            cwd=tmp_path,
            max_tool_result_bytes=1024,
            download_timeout_s=10,
        ),
    )

    pointer = re.search(r"the full text is at (.+) — read it", result)
    assert pointer is not None
    assert Path(pointer.group(1)).read_bytes() == _TEXT_PAYLOAD
    assert len(_ranges(state, "/text")) >= 2


def test_tool_installer_download_uses_segmented_aria2_transfer(
    transfer_server, tmp_path: Path
) -> None:
    base_url, state = transfer_server
    _clear_server_state(state)
    target = tmp_path / "release-asset.tar.gz"

    tool_binaries._download(f"{base_url}/file", target)

    assert target.read_bytes() == _PAYLOAD
    assert len(_ranges(state, "/file")) >= 2


def test_interrupted_transfer_resumes_hidden_partial(
    transfer_server, aria2c: str, tmp_path: Path
) -> None:
    base_url, state = transfer_server
    _clear_server_state(state)
    state.slow = True
    target = tmp_path / "resumed.bin"

    with pytest.raises(tool_binaries.DownloadError, match="timed out"):
        tool_binaries.download_with_aria2(
            aria2c,
            f"{base_url}/slow",
            target,
            timeout_s=0.25,
        )
    partial = tool_binaries._partial_download_path(target)
    assert partial.stat().st_size > 0
    first_bytes = state.bytes_sent

    _clear_server_state(state)
    state.slow = False
    tool_binaries.download_with_aria2(
        aria2c,
        f"{base_url}/slow",
        target,
        timeout_s=10,
    )

    assert target.read_bytes() == _PAYLOAD
    assert first_bytes > 0
    assert len(_ranges(state, "/slow")) >= 2


def test_redirect_is_followed_by_real_transfer(
    transfer_server, aria2c: str, tmp_path: Path
) -> None:
    base_url, _state = transfer_server
    target = tmp_path / "redirected.bin"

    tool_binaries.download_with_aria2(
        aria2c,
        f"{base_url}/redirect",
        target,
        timeout_s=10,
    )

    assert target.read_bytes() == _PAYLOAD


@pytest.mark.parametrize(
    ("path", "message"),
    [("truncated", "exited")],
)
def test_failed_http_transfer_keeps_existing_complete_destination(
    transfer_server,
    aria2c: str,
    tmp_path: Path,
    path: str,
    message: str,
) -> None:
    base_url, _state = transfer_server
    target = tmp_path / f"{path}.bin"
    target.write_bytes(b"known-complete-file")

    with pytest.raises(tool_binaries.DownloadError, match=message):
        tool_binaries.download_with_aria2(
            aria2c,
            f"{base_url}/{path}",
            target,
            timeout_s=5,
        )

    assert target.read_bytes() == b"known-complete-file"


def test_http_404_returns_plain_error_and_preserves_complete_destination(
    transfer_server, tmp_path: Path
) -> None:
    base_url, _state = transfer_server
    target = tmp_path / "missing.bin"
    target.write_bytes(b"known-complete-file")

    result = process_net.fetch(
        f"{base_url}/missing",
        save=target.name,
        context=ToolContext(cwd=tmp_path, download_timeout_s=5),
    )

    assert result.startswith("ERROR: aria2c exited 3 downloading ")
    assert target.read_bytes() == b"known-complete-file"


def test_slow_fetch_obeys_download_timeout_and_preserves_complete_destination(
    transfer_server, tmp_path: Path
) -> None:
    base_url, state = transfer_server
    state.slow = True
    target = tmp_path / "slow.bin"
    target.write_bytes(b"known-complete-file")
    started = time.monotonic()

    result = process_net.fetch(
        f"{base_url}/slow",
        save=target.name,
        context=ToolContext(cwd=tmp_path, download_timeout_s=0.25),
    )

    assert target.read_bytes() == b"known-complete-file"
    assert result.startswith("ERROR: aria2c timed out after 0.25s downloading ")
    assert time.monotonic() - started < 1.5


def test_fetch_download_cap_stops_transfer_before_publishing(
    transfer_server, monkeypatch, tmp_path: Path
) -> None:
    base_url, _state = transfer_server
    target = tmp_path / "capped.bin"
    target.write_bytes(b"known-complete-file")

    result = process_net.fetch(
        f"{base_url}/file",
        save=target.name,
        context=ToolContext(
            cwd=tmp_path, download_timeout_s=10, max_download_bytes=1024 * 1024
        ),
    )

    assert result == "ERROR: response exceeds 1048576 byte download limit"
    assert target.read_bytes() == b"known-complete-file"


def test_aria2_argv_enables_segmentation_resume_retry_and_quiet_output(
    aria2c: str, tmp_path: Path
) -> None:
    argv = tool_binaries.aria2_argv(
        aria2c,
        "https://example.test/file",
        tmp_path / ".file.aria2-part",
        timeout_s=12,
        headers={"X-Test": "yes"},
    )

    assert "--continue=true" in argv
    assert "--split=8" in argv
    assert "--max-connection-per-server=8" in argv
    assert "--min-split-size=1M" in argv
    assert "--max-tries=5" in argv
    assert "--retry-wait=1" in argv
    assert "--connect-timeout=10" in argv
    assert "--timeout=12" in argv
    assert "--enable-color=false" in argv
    assert "--console-log-level=warn" in argv
    assert "--summary-interval=0" in argv
    assert "--download-result=hide" in argv
    assert "--header=X-Test: yes" in argv


def test_aria2c_is_a_known_system_tool() -> None:
    assert tool_binaries.SYSTEM_TOOLS[tool_binaries.ARIA2_EXECUTABLE] == "1.37.0"


def test_missing_aria2_fallback_is_visible_and_preserves_binary_result(
    transfer_server, monkeypatch, tmp_path: Path
) -> None:
    base_url, _state = transfer_server
    monkeypatch.setattr(process_net, "resolve_binary", lambda _name: None)

    with pytest.warns(RuntimeWarning, match="falling back to urllib"):
        result = process_net.fetch(
            f"{base_url}/file",
            context=ToolContext(cwd=tmp_path),
        )

    assert result == (
        f"BINARY_RESPONSE content-type=application/octet-stream size={len(_PAYLOAD)} bytes "
        "[truncated]"
    )
