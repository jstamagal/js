"""Web search, library docs, and rendered-page tools.

Four search backends and one reader, kept as separate tools so prompt
frontmatter can hand each agent exactly the surface it needs. Every backend
reads its API key from the environment at call time and fails with a plain
ERROR string when the key is missing, so a surface can carry a tool the
current shell cannot use without breaking registry assembly.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.parse
import urllib.request
from typing import Any

from ..capped_process import CappedProcessResult, _run_capped, truncation_marker
from .core import Tool, ToolContext
from .descriptions import load_description
from .sanitize import int_or_default, text_or_default


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float,
) -> Any:
    """One JSON round-trip. Returns parsed JSON, or an ERROR string."""
    data = None
    all_headers = {"User-Agent": "js-agent/0.1", **(headers or {})}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        all_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return f"ERROR: HTTP {exc.code} from {urllib.parse.urlparse(url).netloc}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"
    try:
        return json.loads(body)
    except ValueError:
        return f"ERROR: non-JSON response from {urllib.parse.urlparse(url).netloc}"


def _key(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _cap(text: str, context: ToolContext) -> str:
    limit = context.max_tool_result_bytes
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") + "\n[truncated]"


def _numbered(results: list[dict], *, url_key: str, body_key: str) -> str:
    lines: list[str] = []
    for i, hit in enumerate(results, 1):
        title = str(hit.get("title") or "").strip()
        url = str(hit.get(url_key) or "").strip()
        body = str(hit.get(body_key) or "").strip()
        lines.append(f"{i}. {title}\n{url}" + (f"\n{body}" if body else ""))
    return "\n\n".join(lines) if lines else "no results"


def serper_search(
    query: str,
    num: int | None = 8,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    query = text_or_default(query)
    if not query:
        return "ERROR: query is required"
    key = _key("SERPER_API_KEY")
    if key is None:
        return "ERROR: SERPER_API_KEY is not set"
    reply = _http_json(
        "https://google.serper.dev/search",
        method="POST",
        headers={"X-API-KEY": key},
        payload={"q": query, "num": int_or_default(num, 8, minimum=1)},
        timeout=context.fetch_timeout_s,
    )
    if isinstance(reply, str):
        return reply
    organic = reply.get("organic") or []
    box = reply.get("answerBox") or {}
    parts = []
    answer = str(box.get("answer") or box.get("snippet") or "").strip()
    if answer:
        parts.append(f"answer box: {answer}")
    parts.append(_numbered(organic, url_key="link", body_key="snippet"))
    return _cap("\n\n".join(parts), context)


def tavily_search(
    query: str,
    max_results: int | None = 8,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    query = text_or_default(query)
    if not query:
        return "ERROR: query is required"
    key = _key("TAVILY_API_KEY")
    if key is None:
        return "ERROR: TAVILY_API_KEY is not set"
    reply = _http_json(
        "https://api.tavily.com/search",
        method="POST",
        headers={"Authorization": f"Bearer {key}"},
        payload={
            "query": query,
            "max_results": int_or_default(max_results, 8, minimum=1),
            "include_answer": True,
        },
        timeout=context.fetch_timeout_s,
    )
    if isinstance(reply, str):
        return reply
    parts = []
    answer = str(reply.get("answer") or "").strip()
    if answer:
        parts.append(f"answer: {answer}")
    parts.append(_numbered(reply.get("results") or [], url_key="url", body_key="content"))
    return _cap("\n\n".join(parts), context)


def exa_search(
    query: str,
    num: int | None = 8,
    text_chars: int | None = 1500,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    query = text_or_default(query)
    if not query:
        return "ERROR: query is required"
    key = _key("EXA_API_KEY")
    if key is None:
        return "ERROR: EXA_API_KEY is not set"
    reply = _http_json(
        "https://api.exa.ai/search",
        method="POST",
        headers={"x-api-key": key},
        payload={
            "query": query,
            "numResults": int_or_default(num, 8, minimum=1),
            "type": "auto",
            "contents": {"text": {"maxCharacters": int_or_default(text_chars, 1500, minimum=100)}},
        },
        timeout=context.fetch_timeout_s,
    )
    if isinstance(reply, str):
        return reply
    return _cap(_numbered(reply.get("results") or [], url_key="url", body_key="text"), context)


def docs_search(
    library: str,
    topic: str | None = "",
    tokens: int | None = 4000,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    library = text_or_default(library)
    if not library:
        return "ERROR: library is required"
    headers: dict[str, str] = {}
    key = _key("CONTEXT7_API_KEY")
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"

    found = _http_json(
        "https://context7.com/api/v1/search?query=" + urllib.parse.quote(library),
        headers=headers,
        timeout=context.fetch_timeout_s,
    )
    if isinstance(found, str):
        return found
    results = found.get("results") or []
    if not results:
        return f"no library on context7 matches {library!r}"
    best = results[0]
    library_id = str(best.get("id") or "").strip()
    if not library_id:
        return f"no library on context7 matches {library!r}"

    params = {"type": "txt", "tokens": str(int_or_default(tokens, 4000, minimum=500))}
    topic = text_or_default(topic)
    if topic:
        params["topic"] = topic
    doc_url = f"https://context7.com/api/v1{library_id}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(doc_url, headers={"User-Agent": "js-agent/0.1", **headers})
    try:
        with urllib.request.urlopen(req, timeout=context.fetch_timeout_s) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return f"ERROR: HTTP {exc.code} fetching context7 docs for {library_id}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"
    header = f"[context7 {library_id}" + (f", topic: {topic}" if topic else "") + "]\n"
    return _cap(header + text, context)


_PRIVATE_HOSTS = ("localhost", "127.", "0.0.0.0", "10.", "192.168.", "172.16.", "[::1]")


def browse(
    url: str,
    dump: str | None = "markdown",
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    url = text_or_default(url)
    if not url:
        return "ERROR: url is required"
    binary = shutil.which("obscura")
    if binary is None:
        return "ERROR: obscura is not installed (expected on PATH)"
    dump = text_or_default(dump, "markdown") or "markdown"
    if dump not in ("markdown", "text", "html", "links"):
        return "ERROR: dump must be one of markdown, text, html, links"
    argv = [binary, "fetch", "--dump", dump]
    host = urllib.parse.urlparse(url).netloc.split("@")[-1].lower()
    if any(host.startswith(prefix) for prefix in _PRIVATE_HOSTS):
        argv.append("--allow-private-network")
    argv.append(url)
    try:
        result = _run_capped(
            argv,
            timeout=int(context.fetch_timeout_s),
            cwd=str(context.cwd),
            cap=context.max_tool_result_bytes,
        )
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"
    if isinstance(result, CappedProcessResult):
        code, raw_out, raw_err = result.returncode, result.stdout, result.stderr
        out_truncated = result.stdout_truncated
    else:
        code, raw_out, raw_err = result
        out_truncated = False
    stdout = raw_out.decode("utf-8", errors="replace").strip()
    stderr = raw_err.decode("utf-8", errors="replace").strip()
    if code != 0:
        return f"ERROR: obscura exited {code}: {stderr[-300:] or stdout[-300:]}"
    if out_truncated:
        stdout = f"{stdout}\n{truncation_marker(context.max_tool_result_bytes)}"
    return stdout or "(empty page)"


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "serper_search",
            load_description("serper_search"),
            serper_search,
            {
                "query": {"type": "string"},
                "num": {"type": "integer", "default": 8},
            },
            required=("query",),
        ),
        Tool(
            "tavily_search",
            load_description("tavily_search"),
            tavily_search,
            {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 8},
            },
            required=("query",),
        ),
        Tool(
            "exa_search",
            load_description("exa_search"),
            exa_search,
            {
                "query": {"type": "string"},
                "num": {"type": "integer", "default": 8},
                "text_chars": {"type": "integer", "default": 1500},
            },
            required=("query",),
        ),
        Tool(
            "docs_search",
            load_description("docs_search"),
            docs_search,
            {
                "library": {"type": "string"},
                "topic": {"type": "string"},
                "tokens": {"type": "integer", "default": 4000},
            },
            required=("library",),
        ),
        Tool(
            "browse",
            load_description("browse"),
            browse,
            {
                "url": {"type": "string"},
                "dump": {"type": "string", "default": "markdown"},
            },
            required=("url",),
        ),
    )
