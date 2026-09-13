"""Loopback HTTP SSE provider driving js's actual SDK adapter and runtime."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

from harness import (
    OUT,
    RESULTS,
    M,
    R,
    Tool,
    ToolContext,
    ToolRegistry,
    cfg_for,
    facts,
    observed,
    persist_turn,
)
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


class Server(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        owner = self.server.owner
        owner.requests.append(body)
        is_summary = len(body["messages"]) == 1 and "Summarize this js session" in str(
            body["messages"][0]["content"]
        )
        if is_summary:
            content = str(body["messages"][0]["content"])
            source = facts(content)
            owner.summary_calls += 1
            if len(source) > 2:
                data = json.dumps(
                    {
                        "error": {
                            "message": "context_length_exceeded",
                            "type": "invalid_request_error",
                            "code": "context_length_exceeded",
                        }
                    }
                ).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            text = " ".join(source) or "short summary"
            delta = {"role": "assistant", "content": text}
            finish = "stop"
            prompt = 100
            cached = 90
            out = 20
        else:
            owner.calls += 1
            if owner.mode == "overflow" and owner.calls == 2:
                data = json.dumps(
                    {
                        "error": {
                            "message": "context_length_exceeded",
                            "type": "invalid_request_error",
                            "code": "context_length_exceeded",
                        }
                    }
                ).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if owner.calls < 4:
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call_{owner.calls}",
                            "type": "function",
                            "function": {"name": "audit_action", "arguments": "{}"},
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "finished"}
                finish = "stop"
            prompt = 63000 if owner.mode == "cached" else 2000
            cached = prompt - 100
            out = 20
        usage = {
            "prompt_tokens": prompt,
            "completion_tokens": out,
            "total_tokens": prompt + out,
            "prompt_tokens_details": {"cached_tokens": cached},
        }
        frames = [
            {
                "id": "chatcmpl-audit",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "audit",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "chatcmpl-audit",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "audit",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            },
            {
                "id": "chatcmpl-audit",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "audit",
                "choices": [],
                "usage": usage,
            },
        ]
        payload = (
            "".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


async def wire(mode):
    owner = SimpleNamespace(mode=mode, calls=0, summary_calls=0, requests=[])
    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    server.owner = owner
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = cfg_for("wire_" + mode, context_window=128000, tail_tokens=100)
    cfg = replace(cfg, provider_base_url=f"http://127.0.0.1:{server.server_port}/v1")
    context = ToolContext(cwd=cfg.agent_dir)
    effects = []

    def action(context):
        effects.append("effect")
        return "tool data"

    registry = ToolRegistry(
        tools=(Tool("audit_action", "fixture action", action, params={}),), aliases={}
    )
    messages = [
        {"role": "user" if n % 2 == 0 else "assistant", "content": f"FACT_{n} " + "x " * 400}
        for n in range(8)
    ]
    messages.append({"role": "user", "content": "run tools"})
    user = messages[-1]
    for message in messages:
        M.append_message(cfg.session_file, message)
    stats = []
    error = None
    try:
        with patch.object(R.T, "DEFAULT_CONTEXT", context):
            await R.run_turn_async(
                cfg,
                "",
                messages,
                R.Telemetry(cfg.agent_dir / "events.jsonl"),
                tool_registry=registry,
                tool_context=context,
                suppress_output=True,
                call_stats=stats,
            )
            persist_turn(cfg, messages, user)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()
    expected_effects = 2 if mode == "overflow" else 3
    observed(
        "wire_" + mode,
        {
            "finishes": error is None and messages[-1]["content"] == "finished",
            "effects_once": len(effects) == expected_effects,
            "reload_matches": M.load_messages(cfg.session_file) == messages,
            "cache_input_inclusive": all(
                s["prompt_tokens"] == 63000 and s["cached_tokens"] == 62900 for s in stats
            )
            if mode == "cached"
            else True,
            "compaction_count": owner.summary_calls == 0
            if mode == "cached"
            else owner.summary_calls > 0,
        },
        error=error,
        calls=owner.calls,
        summaries=owner.summary_calls,
        effects=len(effects),
        stats=stats,
    )
    (cfg.agent_dir / "wire-requests.json").write_text(json.dumps(owner.requests, indent=2))


async def go():
    for mode in ("cached", "overflow"):
        await wire(mode)
    (OUT / "wire-results.json").write_text(json.dumps(RESULTS, indent=2))
    return all(r["pass"] for r in RESULTS)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(go()) else 1)
