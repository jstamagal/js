"""HYMEM: long-term memory from a Hy-Memory server (`js/hymem_server.py`),
on while HYMEM_URL (`JS_HYMEM_URL=http://vader:19527`) and Jev's
TYPESAFE_API_KEY are both set.

On, a run's session is sent to the server 10 user turns at a time (the chunk
size the model was trained on) as the run goes, and the rest when it ends; the
first message of a `js -p` run or a swarm agent carries the memories that match
its task; the `memory_search` tool searches them mid-task. A memory is passed
on only when Jev judges it helps the task (HYMEM_RELEVANCE_MIN). What js itself put
in front of the model, the memory block and memory_search results, is never
sent back to be filed. Each memory carries the date it was written and, when
the files it names have commits since, how many, so a note that may be stale
says so. Memories are keyed by repository: the main checkout and its
worktrees share one set. Memory works whole or not at all: when the server or
Jev cannot answer, the run goes on with no memories, never with unjudged ones.
What a server that is down was not sent is sent by a later run."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

from . import memory as M
from . import paths as _paths
from . import settings as _settings

CHUNK_TURNS = 10
CLIP = 1500                     # chars kept of each user or assistant message sent
TOOL_CLIP = 300                 # chars kept of each tool result: lessons live in the talk, not the output
SEARCH_TIMEOUT = 8              # seconds a search waits for the server
ADD_TIMEOUT = 5                 # seconds an add waits; the server only queues it
TOKEN_ENV = "HYMEM_TOKEN"       # the memory server's bearer token, as js/hymem_server.py reads it
PATH_IN_TEXT = re.compile(r"(?<![\w/~.-])(?:[\w.-]+/)*[\w.-]+\.\w{1,8}(?=[\s:`'\"),;]|$)")


class Unavailable(Exception):
    """Memory is on but the server or Jev cannot answer."""


BLOCK = re.compile(r"\n*<workspace_memory\b.*?</workspace_memory>\n*", re.S)


def _url(cfg) -> str:
    """``cfg`` is a Config or a ToolContext: both carry the HYMEM rows."""
    return str(_settings.knob_attr(cfg, "hymem_url", "HYMEM_URL") or "").rstrip("/")


def enabled(cfg) -> bool:
    """On while HYMEM_URL and TYPESAFE_API_KEY are both set: unset either to turn memory off."""
    from .toolkit import jev
    return bool(_url(cfg)) and bool(os.environ.get(jev.API_KEY_ENV, "").strip())


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
    """POST ``body`` to the memory server, with HYMEM_TOKEN from the
    environment as its bearer token when set."""
    headers = {"Content-Type": "application/json"}
    if token := os.environ.get(TOKEN_ENV, "").strip():
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(_url(cfg) + path, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _user(cfg) -> str:
    return str(_settings.knob_attr(cfg, "hymem_user", "HYMEM_USER") or getpass.getuser())


def _number(cfg, attr: str, key: str) -> float:
    return float(_settings.knob_attr(cfg, attr, key))


def _relevance(cfg, query: str, memories: list[str]) -> list[float]:
    """Jev's 0-1 judgment of whether each memory helps ``query``, in one call."""
    from .toolkit import jev
    state = {"task": query[:4000], "memories": {f"m{i}": m for i, m in enumerate(memories, 1)}}
    asked = {f"q{i}": {"type": "noul", "instructions": f"Would `memories.m{i}` help an agent doing `task`?"}
             for i in range(1, len(memories) + 1)}
    try:
        with jev._client(getattr(cfg, "jev_model", None) or "jev-latest") as client:
            answers = client.system_one(state, asked).answers
        return [float(jev._field(answers[f"q{i}"], "noul")) for i in range(1, len(memories) + 1)]
    except Exception as exc:  # noqa: BLE001 - no key, no SDK or no answer
        raise Unavailable(f"Jev did not judge the memories: {exc}") from exc


def changed_since(repo: str, files: list, written: str) -> tuple[int, list[str]]:
    """(commits, file names) touching ``files`` in ``repo`` after ``written``,
    an ISO time in UTC as the server stamps it. Directories and paths outside
    the repository are not counted."""
    root = Path(repo)
    paths: list[str] = []
    for name in dict.fromkeys(files or []):
        path = Path(str(name)).expanduser()
        path = (path if path.is_absolute() else root / path).resolve()
        if path.is_file() and path.is_relative_to(root) and str(path.relative_to(root)) not in paths:
            paths.append(str(path.relative_to(root)))
    if not paths or not written:
        return 0, []
    since = f"--since={written[:19].replace('T', ' ')} +0000"
    try:
        log = subprocess.run(["git", "log", "--format=%h", since, "--", *paths], cwd=root,
                             capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return 0, []
    commits = log.stdout.split() if log.returncode == 0 else []
    return len(commits), [Path(p).name for p in paths] if commits else []


def _dated(repo: str, memory: dict, text: str, written) -> str:
    """``text`` led by when it was written and, if the files it names, in its
    record or in its text, changed since, by how much."""
    if not written:
        return text
    files = [*(memory.get("files") or []), *PATH_IN_TEXT.findall(text)]
    commits, names = changed_since(repo, files, str(written))
    stale = f"; {', '.join(names[:3])} changed in {commits} commit{'s' * (commits != 1)} since" if commits else ""
    return f"(written {str(written)[:10]}{stale}) {text}"


def search(cfg, query: str, cwd: str | Path, limit: int | None = None) -> list[str]:
    """One line per memory that helps ``query`` in ``cwd``'s repository, most
    helpful first. Raises Unavailable when the server or Jev cannot answer."""
    limit = limit or int(_settings.knob_attr(cfg, "hymem_recall_max", "HYMEM_RECALL_MAX") or 8)
    try:
        r = _post(cfg, "/search", {"query": query[:2000], "user_id": _user(cfg), "workspace_id": workspace(cwd),
                                   "scene": "coding", "limit": limit * 2}, SEARCH_TIMEOUT)
    except (OSError, ValueError) as exc:
        raise Unavailable(f"the memory server at {_url(cfg)} did not answer: {exc}") from exc
    repo = workspace(cwd)
    found = [(float(e.get("score") or 0),
              _dated(repo, e, f"{e['task']}: {str(e.get('solution') or '')[:600]}", e.get("updated_at")))
             for e in r.get("experiences") or [] if e.get("task")]
    found += [(float(f.get("score") or 0), _dated(repo, f, str(f["statement"])[:400], f.get("created_at")))
              for f in r.get("facts") or [] if f.get("statement")]
    if not found:
        return []
    judged = _relevance(cfg, query, [text for _, text in found])
    floor = _number(cfg, "hymem_relevance_min", "HYMEM_RELEVANCE_MIN")
    ranked = sorted(((j, text) for j, (_, text) in zip(judged, found)), key=lambda m: -m[0])
    return [f"- {text}" for value, text in ranked if value >= floor][:limit]


def recall_block(cfg, query: str, cwd: str | Path) -> str:
    """The memories matching ``query`` as one block for a first message, or ""."""
    if not enabled(cfg) or not query.strip():
        return ""
    try:
        lines = search(cfg, query, cwd)
    except Unavailable:
        return ""
    if not lines:
        return ""
    return "\n".join([f'<workspace_memory repo="{workspace(cwd)}">',
                      "From earlier sessions in this repository. Check one against the files before relying on it.",
                      *lines, "</workspace_memory>"])


def _messages(session: Path) -> list[dict]:
    """The session's messages in OpenAI shape, bodies clipped: its whole
    history, compacted turns included, without what a rollback took back."""
    out = []
    for m in M.retained_messages(session):
        keep = {k: m[k] for k in ("role", "content", "tool_calls", "tool_call_id", "name") if k in m}
        if isinstance(keep.get("content"), list):
            keep["content"] = "\n".join(p.get("text", "") for p in keep["content"] if isinstance(p, dict))
        out.append(keep)
    return _unrecalled(out)


def _clipped_call(call):
    """A tool call with its arguments clipped: a write carries a whole file."""
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict) or not isinstance(function.get("arguments"), str):
        return call
    return {**call, "function": {**function, "arguments": function["arguments"][:TOOL_CLIP]}}


def _unrecalled(messages: list[dict]) -> list[dict]:
    """``messages`` without what memory itself put there (the memory block,
    memory_search calls and their results), bodies clipped."""
    recalled = {c.get("id") for m in messages for c in m.get("tool_calls") or []
                if isinstance(c, dict) and (c.get("function") or {}).get("name") == "memory_search"}
    out = []
    for m in messages:
        if m.get("role") == "tool" and (m.get("name") == "memory_search" or m.get("tool_call_id") in recalled):
            continue
        m = dict(m)
        if m.get("tool_calls"):
            m["tool_calls"] = [_clipped_call(c) for c in m["tool_calls"]
                               if not (isinstance(c, dict) and c.get("id") in recalled)]
            if not m["tool_calls"]:
                del m["tool_calls"]
        if isinstance(m.get("content"), str):
            m["content"] = BLOCK.sub("\n\n", m["content"]).strip()[:TOOL_CLIP if m.get("role") == "tool" else CLIP]
        out.append(m)
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


def _key(message: dict) -> str:
    return hashlib.sha256(json.dumps(message, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _resume_at(mark: Path, messages: list[dict], keys: list[str]) -> int | None:
    """Where filing goes on from: after what the mark says was sent, or, when
    a rollback took back part of that, at the start of the turn it changed.
    None while that rollback has nothing new after it yet. The mark holds the
    key of each message sent; one that cannot be read counts as nothing sent."""
    try:
        sent = json.loads(mark.read_text()) if mark.is_file() else []
    except (OSError, ValueError):
        sent = []
    if isinstance(sent, int):           # marks written before the keys: a message count
        return min(sent, len(messages))
    if not isinstance(sent, list):
        sent = []
    same = 0
    for old, new in zip(sent, keys):
        if old != new:
            break
        same += 1
    if same == len(sent):
        return same
    if same == len(messages):
        return None
    while same > 0 and messages[same].get("role") != "user":
        same -= 1
    return same


def file(cfg, cwd: str | Path, final: bool) -> None:
    """Send what the server has not had of the session: whole chunks, and the
    rest when ``final``. Stops at the first add the server does not take."""
    session = Path(str(getattr(cfg, "session_file", "") or ""))
    if not enabled(cfg) or not session.is_file():
        return
    mark = _paths.state_root() / "hymem" / (session.stem + ".sent")
    messages = _messages(session)
    keys = [_key(m) for m in messages]
    start = _resume_at(mark, messages, keys)
    if start is None:
        return
    for end, chunk in chunks(messages, start, final):
        try:
            _post(cfg, "/add", {"messages": chunk, "user_id": _user(cfg), "workspace_id": workspace(cwd),
                                "scene": "coding", "session_id": session.stem}, ADD_TIMEOUT)
        except (OSError, ValueError):
            return
        mark.parent.mkdir(parents=True, exist_ok=True)
        partial = mark.with_suffix(".sent.tmp")
        partial.write_text(json.dumps(keys[:end]))
        partial.replace(mark)
