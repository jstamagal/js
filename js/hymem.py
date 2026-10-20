"""HYMEM: long-term memory from a Hy-Memory server (`js/hymem_server.py`),
on while HYMEM_URL is set (`JS_HYMEM_URL=http://vader:19527`).

On, a run's session is sent to the server 10 user turns at a time (the chunk
size the model was trained on) as the run goes, and the rest when it ends; the
first message of a `js -p` run or a swarm agent carries the memories that match
its task; the `memory_search` tool searches them mid-task. Memories are keyed
by repository: the main checkout and its worktrees share one set. A server
that does not answer costs one short timeout: no memories are added, and what
was not sent is sent by a later run."""

from __future__ import annotations

import getpass
import json
import subprocess
import urllib.request
from pathlib import Path

from . import paths as _paths
from . import settings as _settings

CHUNK_TURNS = 10
CLIP = 1500                     # chars kept of each message body sent
SEARCH_TIMEOUT = 8              # seconds a search waits for the server
ADD_TIMEOUT = 5                 # seconds an add waits; the server only queues it


def _url(cfg) -> str:
    """``cfg`` is a Config or a ToolContext: both carry the HYMEM rows."""
    return str(_settings.knob_attr(cfg, "hymem_url", "HYMEM_URL") or "").rstrip("/")


def enabled(cfg) -> bool:
    return bool(_url(cfg))


def workspace(cwd: str | Path) -> str:
    """The repository ``cwd`` belongs to (a worktree resolves to its main
    checkout), or ``cwd`` itself outside git."""
    cwd = Path(cwd).expanduser().resolve()
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd,
                                capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return str(cwd)
    if common.returncode != 0 or not common.stdout.strip():
        return str(cwd)
    git_dir = Path(common.stdout.strip())
    return str(git_dir.parent if git_dir.name == ".git" else git_dir)


def _post(cfg, path: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(_url(cfg) + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _user(cfg) -> str:
    return str(_settings.knob_attr(cfg, "hymem_user", "HYMEM_USER") or getpass.getuser())


def search(cfg, query: str, cwd: str | Path, limit: int | None = None) -> list[str]:
    """One line per memory matching ``query`` in ``cwd``'s repository. Raises
    OSError or ValueError when the server cannot answer."""
    limit = limit or int(_settings.knob_attr(cfg, "hymem_recall_max", "HYMEM_RECALL_MAX") or 8)
    r = _post(cfg, "/search", {"query": query[:2000], "user_id": _user(cfg), "workspace_id": workspace(cwd),
                               "scene": "coding", "limit": limit}, SEARCH_TIMEOUT)
    lines = [f"- {e['task']}: {str(e.get('solution') or '')[:600]}"
             for e in r.get("experiences") or [] if e.get("task")]
    lines += [f"- {f['statement'][:600]}" for f in r.get("facts") or [] if f.get("statement")]
    return lines


def recall_block(cfg, query: str, cwd: str | Path) -> str:
    """The memories matching ``query`` as one block for a first message, or ""."""
    if not enabled(cfg) or not query.strip():
        return ""
    try:
        lines = search(cfg, query, cwd)
    except (OSError, ValueError):
        return ""
    if not lines:
        return ""
    return "\n".join([f'<workspace_memory repo="{workspace(cwd)}">',
                      "From earlier sessions in this repository. Check one against the files before relying on it.",
                      *lines, "</workspace_memory>"])


def _messages(session: Path) -> list[dict]:
    """The session's messages in OpenAI shape, bodies clipped."""
    out = []
    for line in session.open(errors="replace"):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict) or o.get("kind") != "message" or not isinstance(o.get("message"), dict):
            continue
        m = o["message"]
        keep = {k: m[k] for k in ("role", "content", "tool_calls", "tool_call_id", "name") if k in m}
        if isinstance(keep.get("content"), list):
            keep["content"] = "\n".join(p.get("text", "") for p in keep["content"] if isinstance(p, dict))
        if isinstance(keep.get("content"), str):
            keep["content"] = keep["content"][:CLIP]
        out.append(keep)
    return out


def chunks(messages: list[dict], start: int, final: bool):
    """(end index, chunk) for each run of CHUNK_TURNS user turns from ``start``;
    the last, shorter run only when ``final``."""
    users = [i for i in range(start, len(messages)) if messages[i].get("role") == "user"]
    for n in range(0, len(users), CHUNK_TURNS):
        if n + CHUNK_TURNS < len(users):
            yield users[n + CHUNK_TURNS], messages[users[n]:users[n + CHUNK_TURNS]]
        elif final:
            yield len(messages), messages[users[n]:]


def file(cfg, cwd: str | Path, final: bool) -> None:
    """Send what the server has not had of the session: whole chunks, and the
    rest when ``final``. Stops at the first add the server does not take."""
    session = Path(str(getattr(cfg, "session_file", "") or ""))
    if not enabled(cfg) or not session.is_file():
        return
    mark = _paths.state_root() / "hymem" / (session.stem + ".sent")
    sent = int(mark.read_text()) if mark.is_file() else 0
    for end, chunk in chunks(_messages(session), sent, final):
        try:
            _post(cfg, "/add", {"messages": chunk, "user_id": _user(cfg), "workspace_id": workspace(cwd),
                                "scene": "coding"}, ADD_TIMEOUT)
        except (OSError, ValueError):
            return
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(str(end))
