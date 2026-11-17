from __future__ import annotations

import asyncio
import io
from http.client import HTTPResponse, IncompleteRead
from unittest.mock import Mock

import pytest

from js.toolkit import fetch
from js.toolkit.core import ToolContext


class _Response:
    """An HTTP response whose body reads come from ``chunks``; an exception
    among them is raised by that read."""

    headers: dict[str, str] = {}

    def __init__(self, chunks) -> None:
        self._chunks = iter(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        chunk = next(self._chunks)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk


def _save(monkeypatch, context: ToolContext, response, save: str) -> str:
    """`fetch(save=save)` of an http URL that answers ``response``, without aria2c."""
    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *_args, **_kwargs: response)
    return fetch.fetch("https://example.test/item", save=save, context=context,
                       download_resolver=lambda _name: None)


@pytest.mark.parametrize("error", [IncompleteRead(b"short"), KeyboardInterrupt(), asyncio.CancelledError()])
def test_interrupted_download_removes_partial_and_preserves_destination(tmp_path, monkeypatch, error):
    target = tmp_path / "output.bin"
    target.write_bytes(b"original")
    context = ToolContext(cwd=tmp_path)
    response = _Response([b"first chunk", error])

    if isinstance(error, Exception):
        assert _save(monkeypatch, context, response, "output.bin").startswith("ERROR")
    else:
        with pytest.raises(type(error)) as caught:
            _save(monkeypatch, context, response, "output.bin")
        assert caught.value is error

    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob(".*.js-partial-*")) == []


def test_cut_chunked_http_response_removes_partial(tmp_path, monkeypatch):
    wire = io.BytesIO(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\nshort")
    response = HTTPResponse(Mock(makefile=Mock(return_value=wire)))
    response.begin()
    target = tmp_path / "chunked.bin"
    context = ToolContext(cwd=tmp_path)

    assert _save(monkeypatch, context, response, "chunked.bin").startswith("ERROR")

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
        monkeypatch.setattr(fetch.os, "replace", Mock(side_effect=error))

    result = _save(monkeypatch, context, _Response([b"complete", b""]), "output.bin")

    assert result.startswith("ERROR") and "publication failed" in result
    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob(".*.js-partial-*")) == []
