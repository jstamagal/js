"""The memory server: how queued adds are filed, and who may call it."""
from __future__ import annotations

import importlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

with mock.patch.dict(os.environ):
    server = importlib.import_module("js.hymem_server")


class _Writer:
    """A HyMemoryClient whose adds succeed or raise, and that records them."""

    def __init__(self, fails=False):
        self.fails, self.adds = fails, []

    def add(self, messages, **kwargs):
        self.adds.append(kwargs)
        if self.fails:
            raise RuntimeError("context overflow")
        return {"success": True}


@pytest.fixture
def queue(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "QUEUE", tmp_path / "queue")
    monkeypatch.setattr(server, "FAILED", tmp_path / "failed")
    (tmp_path / "queue").mkdir()
    return tmp_path


def _job(queue, session="s1"):
    body = {"messages": [{"role": "user", "content": "u0"}], "user_id": "u", "session_id": session}
    job = queue / "queue" / "1-a.json"
    job.write_text(json.dumps(body))
    return job, body


def test_an_add_is_filed_under_its_session(monkeypatch, queue):
    writer = _Writer()
    monkeypatch.setattr(server, "client", lambda: writer)
    server.file_job(*_job(queue, session="s1"))
    assert writer.adds[0]["session_id"] == "s1"


def test_an_add_that_keeps_failing_while_others_file_is_set_aside(monkeypatch, queue):
    monkeypatch.setattr(server, "client", lambda: _Writer(fails=True))
    job, _ = _job(queue)
    for _ in range(server.FAILURES_MAX):
        monkeypatch.setattr(server, "_filed_at", time.time() + 1)
        server.file_job(job, json.loads(job.read_text()))
    assert not job.exists() and (queue / "failed" / job.name).exists()


def test_an_add_is_kept_while_the_writer_is_down(monkeypatch, queue):
    monkeypatch.setattr(server, "client", lambda: _Writer(fails=True))
    monkeypatch.setattr(server, "_filed_at", 0.0)
    job, _ = _job(queue)
    for _ in range(server.FAILURES_MAX * 2):
        assert server.file_job(job, json.loads(job.read_text())) is False
    assert job.exists()


def test_with_a_token_set_a_call_without_it_is_refused(monkeypatch, queue):
    monkeypatch.setattr(server, "TOKEN", "sekrit")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/add"
    body = json.dumps({"messages": [{"role": "user", "content": "u0"}], "user_id": "u"}).encode()

    def post(headers):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        assert post({}) == 401
        assert post({"Authorization": "Bearer sekrit"}) == 202
    finally:
        httpd.shutdown()
