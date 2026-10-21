"""The memory server HYMEM talks to. It runs on the memory host under a venv
with `hy-memory` installed, next to the writer LLM and the embedder; js never
imports it.

    python hymem_server.py [--host 0.0.0.0] [--port 19527]

    POST /add     {"messages": [...], "user_id", "workspace_id", "scene"}  -> 202, queued
    POST /search  {"query", "user_id", "workspace_id", "scene", "limit"}   -> facts, experiences
    GET  /health  -> {"status": "ok", "queued": N}

The servers come from the MEMORY_* environment (MEMORY_LLM_BASE_URL,
MEMORY_LLM_MODEL, MEMORY_EMBEDDER_BASE_URL, MEMORY_EMBEDDER_MODEL, ...);
MEMORY_DATA_DIR holds the store and the add queue. FILERS threads file the
queue oldest first, one per writer slot, never two adds of one session at
once, so a session's chunks land in order. A queued add survives a restart.
An add that fails FAILURES_MAX times while other adds file is moved to the
failed directory beside the queue; while none file, the writer is down and
every add waits for it.

With HYMEM_TOKEN set, every call but /health must carry
`Authorization: Bearer <HYMEM_TOKEN>`; js sends it from the same variable."""

import argparse
import json
import os
import pathlib
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = pathlib.Path(os.environ.setdefault("MEMORY_DATA_DIR", str(pathlib.Path.home() / ".hymem" / "data")))
QUEUE = DATA.parent / "queue"
FAILED = DATA.parent / "failed"
SCENES = {"chat", "working", "coding"}
FILERS = int(os.environ.get("HYMEM_FILERS", "4"))   # the writer LLM's parallel slots
FAILURES_MAX = 5
TOKEN = os.environ.get("HYMEM_TOKEN", "")

for key, value in {
    "MEMORY_MODE": "pro",
    "MEMORY_LLM_API_KEY": "none",
    "MEMORY_LLM_EXTRA_BODY": '{"chat_template_kwargs":{"reasoning_effort":"no_think"}}',
    "MEMORY_LLM_TEMPERATURE": "0",
    "MEMORY_EMBEDDER_API_KEY": "none",
    "OPENAI_API_KEY": "none",
}.items():
    os.environ.setdefault(key, value)

_client = None
_client_lock = threading.Lock()
_wake = threading.Condition()
_taken: set[str] = set()                # queue files being filed
_busy: set[str] = set()                 # sessions with an add being filed
_filed_at = 0.0                         # when an add last filed


def embedding_dims() -> int:
    """What the embedder returns, so MEMORY_EMBEDDING_DIMS never disagrees with it."""
    url = os.environ["MEMORY_EMBEDDER_BASE_URL"].rstrip("/") + "/embeddings"
    body = json.dumps({"model": os.environ.get("MEMORY_EMBEDDER_MODEL", ""), "input": "x"}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return len(json.load(r)["data"][0]["embedding"])


def client():
    global _client
    with _client_lock:
        if _client is None:
            os.environ.setdefault("MEMORY_EMBEDDING_DIMS", str(embedding_dims()))
            from hy_memory import HyMemoryClient
            _client = HyMemoryClient(mode="pro")
        return _client


def scene_of(body: dict) -> str | None:
    scene = body.get("scene")
    return scene if scene in SCENES else None


def _next() -> tuple[pathlib.Path, dict, str]:
    """The oldest queued add whose session has none being filed, claimed."""
    with _wake:
        while True:
            for job in sorted(QUEUE.glob("*.json")):
                if job.name in _taken:
                    continue
                try:
                    body = json.loads(job.read_text())
                except (OSError, ValueError):
                    continue
                key = str(body.get("session_id") or job.name)
                if key in _busy:
                    continue
                _taken.add(job.name)
                _busy.add(key)
                return job, body, key
            _wake.wait(60)


def file_job(job: pathlib.Path, body: dict) -> bool:
    """File one queued add. True when the queue is done with it: filed, or
    moved to FAILED. False when it stays queued to be retried; its failures
    are counted in it."""
    global _filed_at
    try:
        r = client().add(body["messages"], scene=scene_of(body), user_id=body["user_id"],
                         session_id=str(body.get("session_id") or "default_session"),
                         workspace_id=body.get("workspace_id"))
        ok = r.get("success", True)
    except Exception as exc:  # noqa: BLE001 - a down LLM is retried, not fatal
        ok, r = False, {"error_message": f"{type(exc).__name__}: {exc}"}
    if ok:
        job.unlink()
        _filed_at = time.time()
        print(f"filed {job.name} ({len(body['messages'])} messages, {body.get('workspace_id')})", flush=True)
        return True
    failures = int(body.get("failures") or 0) + 1
    first = float(body.get("first_failure") or time.time())
    if failures >= FAILURES_MAX and _filed_at > first:
        FAILED.mkdir(parents=True, exist_ok=True)
        job.rename(FAILED / job.name)
        print(f"moved {job.name} to {FAILED} after {failures} failures: {r.get('error_message')}", flush=True)
        return True
    partial = job.with_suffix(".tmp")
    partial.write_text(json.dumps({**body, "failures": failures, "first_failure": first}))
    partial.replace(job)
    print(f"retrying {job.name} in 60s: {r.get('error_message')}", flush=True)
    return False


def filer() -> None:
    """File queued adds; one that fails waits and is retried."""
    while True:
        job, body, key = _next()
        if not file_job(job, body):
            time.sleep(60)
        with _wake:
            _taken.discard(job.name)
            _busy.discard(key)
            _wake.notify_all()


class Handler(BaseHTTPRequestHandler):
    def reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.reply(200, {"status": "ok", "queued": len(list(QUEUE.glob("*.json")))})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if TOKEN and self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.reply(401, {"error": "HYMEM_TOKEN does not match"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.reply(400, {"error": "body is not JSON"})
        if not isinstance(body, dict) or not body.get("user_id"):
            return self.reply(400, {"error": "user_id is required"})
        if self.path == "/add":
            if not isinstance(body.get("messages"), list) or not body["messages"]:
                return self.reply(400, {"error": "messages is required"})
            name = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
            (QUEUE / (name + ".tmp")).write_text(json.dumps(body))
            (QUEUE / (name + ".tmp")).rename(QUEUE / name)
            with _wake:
                _wake.notify_all()
            return self.reply(202, {"queued": name})
        if self.path == "/search":
            if not body.get("query"):
                return self.reply(400, {"error": "query is required"})
            r = client().search(body["query"], scene=scene_of(body), user_ids=[body["user_id"]],
                                workspace_id=body.get("workspace_id"), limit=int(body.get("limit") or 8))
            return self.reply(200, {"facts": r.get("facts") or [], "experiences": r.get("experiences") or []})
        self.reply(404, {"error": "not found"})

    def log_message(self, fmt, *args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=19527)
    args = parser.parse_args()
    QUEUE.mkdir(parents=True, exist_ok=True)
    client()
    for _ in range(FILERS):
        threading.Thread(target=filer, daemon=True).start()
    print(f"hymem server on {args.host}:{args.port}, store {DATA}, {FILERS} filers", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
