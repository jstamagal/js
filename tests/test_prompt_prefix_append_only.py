"""Every request in a session repeats the one before it and adds to the end.

A provider's prompt cache reads the longest prefix it has seen before, so a
request that rewrites anything already sent pays for everything after the
change again. These drive the real runtime and SDK adapter against a loopback
OpenAI-shaped endpoint, the shape CLIProxyAPI serves for both GPT and Claude,
across a tool loop and a second user turn, and compare the request bodies.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from js import memory as M, microcompact, runtime as R
from js.config import Config
from js.toolkit.core import Tool, ToolContext
from js.toolkit.registry import ToolRegistry


def _frames(owner) -> list[dict]:
    owner.calls += 1
    reasoning = {"role": "assistant", "reasoning_content": f"thinking {owner.calls}"}
    if owner.calls in owner.tool_calls_at:
        delta = {
            "tool_calls": [{
                "index": 0,
                "id": f"call_{owner.calls}",
                "type": "function",
                "function": {"name": "probe", "arguments": json.dumps({"n": owner.calls})},
            }],
        }
        finish = "tool_calls"
    else:
        delta = {"content": f"answer {owner.calls}"}
        finish = "stop"
    chunk = {"id": "chatcmpl-prefix", "object": "chat.completion.chunk", "created": 1, "model": owner.model}
    return [
        {**chunk, "choices": [{"index": 0, "delta": reasoning, "finish_reason": None}]},
        {**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        {**chunk, "choices": [], "usage": {
            "prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010,
            "prompt_tokens_details": {"cached_tokens": 900},
        }},
    ]


class _Endpoint(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        owner = self.server.owner
        owner.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        payload = ("".join("data: " + json.dumps(f) + "\n\n" for f in _frames(owner))
                   + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _cfg(tmp_path, port: int, model: str) -> Config:
    return Config(
        agent_id="prefix",
        agent_dir=tmp_path,
        model=model,
        provider_id="cliproxyapi",
        provider_base_url=f"http://127.0.0.1:{port}/v1",
        provider_api_key="fixture",
        reasoning_effort=None,
        max_output_tokens=100,
        max_tool_iterations=8,
        max_bash_output_bytes=100000,
        max_tool_result_bytes=100000,
        fetch_timeout_s=1,
        debug_log=None,
        trace=False,
        history_file=tmp_path / "history.jsonl",
        sessions_dir=tmp_path,
        session_file=tmp_path / "session.jsonl",
        prompts_dir=tmp_path,
        project_dir=tmp_path,
        settings={"COMPACTION_AUTO": False},
    )


def _session_requests(tmp_path, monkeypatch, model: str) -> list[dict]:
    """The request bodies of three user turns: the first runs two tool calls,
    and the third runs on the history read back from the session file, as a
    resumed session does."""
    monkeypatch.setenv("JS_VISION", "false")
    owner = SimpleNamespace(calls=0, tool_calls_at={1, 2}, requests=[], model=model)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Endpoint)
    server.owner = owner
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    cfg = _cfg(tmp_path, server.server_port, model)
    context = ToolContext(cwd=tmp_path)
    registry = ToolRegistry(
        tools=(Tool("probe", "fixture probe", lambda context, n=0: f"probe result {n}",
                    params={"n": {"type": "integer"}}),),
        aliases={},
    )
    messages: list[dict] = []

    async def turn(history: list[dict], text: str) -> None:
        history.append({"role": "user", "content": text})
        await R.run_turn_async(
            cfg, "SYSTEM PROMPT", history, R.Telemetry(debug_log=None),
            tool_registry=registry, tool_context=context, suppress_output=True,
        )
        M.persist_messages(cfg.session_file, history, stamp=microcompact.cfg_stamp(cfg))

    async def drive():
        with patch.object(R.T, "STOCK_CONTEXT", context):
            for text in ("first question", "second question"):
                await turn(messages, text)
            await turn(M.load_replay_messages(cfg.session_file), "third question")

    try:
        asyncio.run(drive())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return owner.requests


MODELS = ("gpt-5.5", "claude-opus-4-8")


@pytest.mark.parametrize("model", MODELS)
def test_each_request_extends_the_one_before(tmp_path, monkeypatch, model):
    requests = _session_requests(tmp_path, monkeypatch, model)

    # Two tool calls and an answer, then the second and third turns' answers.
    assert len(requests) == 5
    for before, after in zip(requests, requests[1:]):
        sent = before["messages"]
        assert after["messages"][:len(sent)] == sent
        assert len(after["messages"]) > len(sent)
        assert after.get("tools") == before.get("tools")


@pytest.mark.parametrize("model", MODELS)
def test_every_request_of_a_session_carries_one_cache_key(tmp_path, monkeypatch, model):
    requests = _session_requests(tmp_path, monkeypatch, model)

    keys = {request.get("prompt_cache_key") for request in requests}
    assert len(keys) == 1
    (key,) = keys
    assert isinstance(key, str) and 0 < len(key) <= 64
