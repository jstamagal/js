#!/usr/bin/env python3
"""Small spec-speaking MCP server used by real stdio transport tests."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def stderr_flood(secret: str) -> None:
    chunk = ("server diagnostic " + secret + "\n") * 1024
    for _ in range(128):
        sys.stderr.write(chunk)
    sys.stderr.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="normal")
    parser.add_argument("--state-file")
    parser.add_argument("--secret-arg", default="")
    args = parser.parse_args()
    secret = os.environ.get("MCP_SENTINEL", "") or args.secret_arg

    if args.mode == "noise":
        print("not json " + secret, flush=True)
        time.sleep(10)
        return
    if args.mode == "stubborn":
        while True:
            time.sleep(1)

    if args.mode == "stderr":
        threading.Thread(target=stderr_flood, args=(secret,), daemon=True).start()

    slow_requests: set[int | str] = set()
    for raw in sys.stdin:
        message = json.loads(raw)
        method = message.get("method")
        params = message.get("params", {})
        request_id = message.get("id")
        if method == "notifications/cancelled":
            slow_requests.discard(params.get("requestId"))
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {"level": "info", "data": "cancelled"},
                }
            )
            continue
        if request_id is None:
            if method == "notifications/initialized":
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/tools/list_changed",
                        "params": {},
                    }
                )
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": True}, "logging": {}},
                "serverInfo": {"name": "fake-stdio", "version": "1"},
            }
        elif method == "tools/list":
            if args.mode == "die-once" and args.state_file:
                marker = Path(args.state_file)
                if not marker.exists():
                    marker.write_text("died", encoding="utf-8")
                    os._exit(23)
            if params.get("cursor") == "page-2":
                result = {"tools": [{"name": "beta", "inputSchema": {"type": "object"}}]}
            else:
                result = {
                    "tools": [{"name": "alpha", "inputSchema": {"type": "object"}}],
                    "nextCursor": "page-2",
                }
        elif method == "tools/call":
            result = {
                "content": [{"type": "text", "text": str(params.get("arguments", {}).get("text"))}],
                "structuredContent": {"called": params.get("name")},
            }
        elif method == "test/slow":
            slow_requests.add(request_id)
            continue
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "unknown method"},
                }
            )
            continue
        send({"jsonrpc": "2.0", "id": request_id, "result": result})


if __name__ == "__main__":
    main()
