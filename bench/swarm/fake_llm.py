"""A fake OpenAI-compatible chat-completions server for swarm benchmarks.

    python bench/swarm/fake_llm.py --port 0 --tokens-per-second 80 --steps 3

Standard library only, so it costs the machine under test as little as it can
and never shares an interpreter with js. Every request is answered with an SSE
stream: a scripted tool call while the conversation has fewer than ``--steps``
assistant tool rounds, then a plain reply of ``--reply-tokens`` tokens. The
tool picked for each round comes from the tools the request offers, so the
same server drives a plain `js -p` agent and a swarm agent on the bus.

A swarm agent's first turn ends by broadcasting one `send`; a turn that starts
from bus messages only replies, so a swarm settles instead of waking itself
forever.

Each request is logged to ``--log`` as one JSON line: when it arrived, when
its stream ended, and the agent's previous stream end, so ``run.py`` can tell
model time from harness time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

WORDS = ("the quick brown fox jumps over a lazy dog while seven agents "
         "argue about tabs and spaces in a terminal ").split()


class Script:
    def __init__(self, args: argparse.Namespace) -> None:
        self.steps = args.steps
        self.reply_tokens = args.reply_tokens
        self.delay = 1.0 / args.tokens_per_second if args.tokens_per_second > 0 else 0.0
        self.chunk = max(1, args.tokens_per_chunk)
        self.read_path = args.read_path
        self.log = open(args.log, "a", encoding="utf-8") if args.log else None  # noqa: SIM115
        self.last_end: dict[str, float] = {}
        self.count = 0

    def plan(self, body: dict) -> tuple[str, dict | None]:
        """(text, tool call or None) for this request."""
        messages = body.get("messages") or []
        tools = {t.get("function", {}).get("name") for t in body.get("tools") or []}
        rounds = sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
        users = [m for m in messages if m.get("role") == "user"]
        woke = len(users) > 1
        since_user = 0
        for m in reversed(messages):
            if m.get("role") == "user":
                break
            if m.get("role") == "assistant" and m.get("tool_calls"):
                since_user += 1
        if since_user < self.steps and not woke:
            order = []
            if "read" in tools:
                order.append(("read", {"file_path": self.read_path}))
            if "fs_search" in tools:
                order.append(("fs_search", {"pattern": "def ", "path": os.path.dirname(self.read_path),
                                            "head_limit": 20}))
            if "shell" in tools:
                order.append(("shell", {"command": "echo hello"}))
            if not order:
                return self.text(), None
            name, args = order[rounds % len(order)]
            return "", {"name": name, "arguments": args}
        if "send" in tools and not woke and since_user == self.steps:
            return "", {"name": "send", "arguments": {"to": "*", "text": "done with my part"}}
        return self.text(), None

    def text(self) -> str:
        return " ".join(WORDS[i % len(WORDS)] for i in range(self.reply_tokens))


def _sse(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"


async def _stream(writer: asyncio.StreamWriter, script: Script, body: dict) -> None:
    text, call = script.plan(body)
    script.count += 1
    rid = f"chatcmpl-{script.count}"
    model = body.get("model") or "fake"
    base = {"id": rid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model}

    def chunk(delta: dict, finish: str | None = None) -> bytes:
        return _sse({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    writer.write(chunk({"role": "assistant", "content": ""}))
    tokens = 0
    if text:
        words = text.split(" ")
        for i in range(0, len(words), script.chunk):
            piece = " ".join(words[i:i + script.chunk]) + " "
            writer.write(chunk({"content": piece}))
            tokens += script.chunk
            await writer.drain()
            if script.delay:
                await asyncio.sleep(script.delay * script.chunk)
    if call is not None:
        cid = f"call_{script.count}"
        arguments = json.dumps(call["arguments"])
        writer.write(chunk({"tool_calls": [{"index": 0, "id": cid, "type": "function",
                                             "function": {"name": call["name"], "arguments": ""}}]}))
        # Arguments stream in small pieces, as real providers send them.
        for i in range(0, len(arguments), 8):
            writer.write(chunk({"tool_calls": [{"index": 0, "function": {"arguments": arguments[i:i + 8]}}]}))
            tokens += 2
            await writer.drain()
            if script.delay:
                await asyncio.sleep(script.delay * 2)
    writer.write(chunk({}, "tool_calls" if call is not None else "stop"))
    prompt_tokens = len(json.dumps(body.get("messages") or [])) // 4
    writer.write(_sse({**base, "choices": [], "usage": {
        "prompt_tokens": prompt_tokens, "completion_tokens": tokens, "total_tokens": prompt_tokens + tokens}}))
    writer.write(b"data: [DONE]\n\n")
    await writer.drain()


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, script: Script) -> None:
    try:
        while True:
            head = await reader.readuntil(b"\r\n\r\n")
            arrived = time.time()
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
            raw = await reader.readexactly(int(headers.get("content-length") or 0))
            if method == "GET" and path.rstrip("/").endswith("/models"):
                payload = json.dumps({"object": "list", "data": [{"id": "fake", "object": "model"}]}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
                await writer.drain()
                continue
            body = json.loads(raw or b"{}")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n")
            await _stream(writer, script, body)
            ended = time.time()
            if script.log is not None:
                agent = headers.get("x-bench-agent") or _agent_of(body)
                script.log.write(json.dumps({"agent": agent, "arrived": arrived, "ended": ended,
                                             "previous_end": script.last_end.get(agent),
                                             "bytes": len(raw)}) + "\n")
                script.log.flush()
                script.last_end[agent] = ended
            break
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


def _agent_of(body: dict) -> str:
    """Which agent sent this: the first user message is its opener, unique per agent."""
    for m in body.get("messages") or []:
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
            return str(content)[:80]
    return "?"


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--tokens-per-second", type=float, default=80.0)
    ap.add_argument("--tokens-per-chunk", type=int, default=1)
    ap.add_argument("--reply-tokens", type=int, default=200)
    ap.add_argument("--steps", type=int, default=3)
    ap.add_argument("--read-path", default=os.path.abspath(__file__))
    ap.add_argument("--log", default="")
    args = ap.parse_args(argv)
    script = Script(args)
    server = await asyncio.start_server(lambda r, w: _handle(r, w, script), "127.0.0.1", args.port, backlog=1024)
    port = server.sockets[0].getsockname()[1]
    print(port, flush=True)
    sys.stdout.close()
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
