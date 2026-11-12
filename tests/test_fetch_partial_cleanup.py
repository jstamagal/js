from __future__ import annotations

import asyncio
import io
from http.client import HTTPResponse, IncompleteRead
from unittest.mock import Mock

import pytest

from js.toolkit import process_net
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("error", [IncompleteRead(b"short"), KeyboardInterrupt(), asyncio.CancelledError()])
def test_interrupted_download_removes_partial_and_preserves_destination(tmp_path, error):
    target = tmp_path / "output.bin"
    target.write_bytes(b"original")
    context = ToolContext(cwd=tmp_path)
    reads = iter([b"first chunk", error])

    def read_chunk(_size):
        result = next(reads)
        if isinstance(result, BaseException):
            raise result
        return result

    with pytest.raises(type(error)) as caught:
        process_net._stream_download(read_chunk, target, "application/octet-stream", context, None)

    assert caught.value is error
    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob(".*.js-partial-*")) == []


def test_cut_chunked_http_response_removes_partial(tmp_path):
    wire = io.BytesIO(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\nshort")
    response = HTTPResponse(Mock(makefile=Mock(return_value=wire)))
    response.begin()
    target = tmp_path / "chunked.bin"
    context = ToolContext(cwd=tmp_path)

    with response, pytest.raises(IncompleteRead):
        process_net._stream_download(response.read, target, "", context, None)

    assert not target.exists()
    assert list(tmp_path.glob(".*.js-partial-*")) == []


@pytest.mark.parametrize("failure_stage", ["snapshot", "replace"])
def test_download_publish_failure_removes_partial(tmp_path, monkeypatch, failure_stage):
    target = tmp_path / "output.bin"
    target.write_bytes(b"original")
    context = ToolContext(cwd=tmp_path)
    error = OSError("publication failed")
    if failure_stage == "snapshot":
        monkeypatch.setattr(context, "snapshot", Mock(side_effect=error))
    else:
        monkeypatch.setattr(process_net.os, "replace", Mock(side_effect=error))
    chunks = iter([b"complete", b""])

    with pytest.raises(OSError) as caught:
        process_net._stream_download(lambda _size: next(chunks), target, "", context, None)

    assert caught.value is error
    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob(".*.js-partial-*")) == []
