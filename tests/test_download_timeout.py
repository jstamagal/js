"""fetch's save= path gets a download budget, not a page-load budget.

_DOWNLOAD_MAX_BYTES allows a 32 MiB download. Sharing the 15s fetch_timeout_s
silently demanded about 2 MB/s sustained to move anything large.
"""
from __future__ import annotations

import pytest

from js.toolkit import ToolContext
from js.toolkit import process_net
from js.toolkit.process_net import fetch


class _FakeResponse:
    headers = {"Content-Type": "application/octet-stream"}
    status = 200

    def read(self, *_args):
        return b"payload"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def seen(monkeypatch):
    box: dict[str, object] = {}

    def fake_urlopen(_req, timeout=None):
        box["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(process_net.urllib.request, "urlopen", fake_urlopen)
    return box


def test_a_saved_download_uses_the_download_budget(tmp_path, seen):
    context = ToolContext(cwd=tmp_path, fetch_timeout_s=15, download_timeout_s=300)
    fetch("https://example.com/big.bin", save="big.bin", context=context)
    assert seen["timeout"] == 300


def test_an_unsaved_fetch_still_uses_the_request_budget(tmp_path, seen):
    context = ToolContext(cwd=tmp_path, fetch_timeout_s=15, download_timeout_s=300)
    fetch("https://example.com/api", context=context)
    assert seen["timeout"] == 15
