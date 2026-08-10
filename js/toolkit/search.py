"""Web search, library docs, and rendered-page tools.

Four search backends and one reader, kept as separate tools so prompt
frontmatter can hand each agent exactly the surface it needs. Every backend
reads its API key from the environment at call time and fails with a plain
ERROR string when the key is missing, so a surface can carry a tool the
current shell cannot use without breaking registry assembly.
"""

from __future__ import annotations

import json
import ipaddress
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
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
    secrets: tuple[str, ...] = (),
    timeout: float,
) -> dict[str, Any] | str:
    """One JSON-object round-trip. Returns a mapping, or an ERROR string."""
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
        for secret in secrets:
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        return f"ERROR: HTTP {exc.code} from {urllib.parse.urlparse(url).netloc}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"
    try:
        parsed = json.loads(body)
    except ValueError:
        return f"ERROR: non-JSON response from {urllib.parse.urlparse(url).netloc}"
    if not isinstance(parsed, dict):
        host = urllib.parse.urlparse(url).netloc
        return f"ERROR: expected a JSON object from {host}, got {_json_kind(parsed)}"
    return parsed


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _key(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _cap(text: str, context: ToolContext) -> str:
    limit = context.max_tool_result_bytes
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") + "\n[truncated]"


def _bounded_int(raw: Any, default: int, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(raw, float) and not raw.is_integer():
        value = default
    else:
        value = int_or_default(raw, default)
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


def _numbered(results: Any, *, url_key: str, body_key: str) -> str:
    if not isinstance(results, list):
        return f"ERROR: expected search results to be an array, got {_json_kind(results)}"
    lines: list[str] = []
    for i, hit in enumerate(results, 1):
        if not isinstance(hit, dict):
            return f"ERROR: expected search result {i} to be an object, got {_json_kind(hit)}"
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
    query = text_or_default(query).strip()
    if not query:
        return "ERROR: query is required"
    key = _key("SERPER_API_KEY")
    if key is None:
        return "ERROR: SERPER_API_KEY is not set"
    reply = _http_json(
        "https://google.serper.dev/search",
        method="POST",
        headers={"X-API-KEY": key},
        payload={"q": query, "num": _bounded_int(num, 8, minimum=1, maximum=100)},
        secrets=(key,),
        timeout=context.fetch_timeout_s,
    )
    if isinstance(reply, str):
        return reply
    organic = reply.get("organic")
    if organic is None:
        organic = []
    numbered = _numbered(organic, url_key="link", body_key="snippet")
    if numbered.startswith("ERROR: "):
        return numbered
    box = reply.get("answerBox")
    if box is None:
        box = {}
    if not isinstance(box, dict):
        return f"ERROR: expected answerBox to be an object, got {_json_kind(box)}"
    parts: list[str] = []
    answer = str(box.get("answer") or box.get("snippet") or "").strip()
    if answer:
        parts.append(f"answer box: {answer}")
    parts.append(numbered)
    return _cap("\n\n".join(parts), context)


def tavily_search(
    query: str,
    max_results: int | None = 8,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    query = text_or_default(query).strip()
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
            "max_results": _bounded_int(max_results, 8, minimum=1, maximum=20),
            "include_answer": True,
        },
        secrets=(key,),
        timeout=context.fetch_timeout_s,
    )
    if isinstance(reply, str):
        return reply
    results = reply.get("results")
    if results is None:
        results = []
    numbered = _numbered(results, url_key="url", body_key="content")
    if numbered.startswith("ERROR: "):
        return numbered
    parts: list[str] = []
    answer = str(reply.get("answer") or "").strip()
    if answer:
        parts.append(f"answer: {answer}")
    parts.append(numbered)
    return _cap("\n\n".join(parts), context)


def exa_search(
    query: str,
    num: int | None = 8,
    text_chars: int | None = 1500,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    query = text_or_default(query).strip()
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
            "numResults": _bounded_int(num, 8, minimum=1, maximum=100),
            "type": "auto",
            "contents": {"text": {"maxCharacters": _bounded_int(text_chars, 1500, minimum=100)}},
        },
        secrets=(key,),
        timeout=context.fetch_timeout_s,
    )
    if isinstance(reply, str):
        return reply
    results = reply.get("results")
    if results is None:
        results = []
    return _cap(_numbered(results, url_key="url", body_key="text"), context)


def docs_search(
    library: str,
    topic: str | None = "",
    tokens: int | None = 4000,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    library = text_or_default(library).strip()
    if not library:
        return "ERROR: library is required"
    headers: dict[str, str] = {}
    key = _key("CONTEXT7_API_KEY")
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"

    found = _http_json(
        "https://context7.com/api/v1/search?query=" + urllib.parse.quote(library),
        headers=headers,
        secrets=(key,) if key is not None else (),
        timeout=context.fetch_timeout_s,
    )
    if isinstance(found, str):
        return found
    results = found.get("results")
    if results is None:
        results = []
    if not isinstance(results, list):
        return f"ERROR: expected search results to be an array, got {_json_kind(results)}"
    if not results:
        return f"no library on context7 matches {library!r}"
    best = results[0]
    if not isinstance(best, dict):
        return f"ERROR: expected search result 1 to be an object, got {_json_kind(best)}"
    library_id = str(best.get("id") or "").strip()
    if not library_id:
        return f"no library on context7 matches {library!r}"

    params = {"type": "txt", "tokens": str(_bounded_int(tokens, 4000, minimum=500))}
    topic = text_or_default(topic).strip()
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
    if not text.strip():
        suffix = f", topic: {topic}" if topic else ""
        return f"ERROR: context7 returned empty docs for {library_id}{suffix}"
    header = f"[context7 {library_id}" + (f", topic: {topic}" if topic else "") + "]\n"
    return _cap(header + text, context)


def _is_private_ip_literal(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname
    if host is None:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _redacted_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return urllib.parse.urlunsplit(parsed._replace(netloc=host, query="", fragment=""))


def _fit_truncation_marker(text: str, cap: int) -> str:
    cap = max(0, cap)
    if cap == 0:
        return ""
    marker = truncation_marker(cap, "limits.max_tool_result_bytes")
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= cap:
        return marker_bytes[:cap].decode("utf-8", errors="ignore")
    content_cap = cap - len(marker_bytes) - 1
    content = text.encode("utf-8")[:content_cap].decode("utf-8", errors="ignore")
    return f"{content}\n{marker}" if content else marker


_BROWSE_DUMPS = ("markdown", "text", "html", "links", "original", "assets", "cookies")

# obscura emits hrefs exactly as the page authored them, so `[Login](/login)` reaches
# the model with nothing to resolve against. Rewriting them against the fetched URL is
# what makes the link list actionable instead of decorative. In-page anchors are left
# alone on purpose — they carry a "same page" signal that a full URL destroys.
_MD_LINK = re.compile(r"(!?\[[^\]]*\])\(([^)\s]+)((?:\s+\"[^\"]*\")?)\)")


def _absolutize(markdown: str, base_url: str) -> str:
    def _fix(match: re.Match[str]) -> str:
        label, target, title = match.group(1), match.group(2), match.group(3)
        if target.startswith("#") or "://" in target or target.startswith(("mailto:", "tel:", "data:")):
            return match.group(0)
        return f"{label}({urllib.parse.urljoin(base_url, target)}{title})"

    return _MD_LINK.sub(_fix, markdown)


def browse(
    url: str,
    dump: str | None = "markdown",
    screenshot: str | None = None,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    url = text_or_default(url).strip()
    if not url:
        return "ERROR: url is required"
    binary = shutil.which("obscura")
    if binary is None:
        return "ERROR: obscura is not installed (expected on PATH)"
    dump = text_or_default(dump, "markdown").strip().lower() or "markdown"
    if dump not in _BROWSE_DUMPS:
        return f"ERROR: dump must be one of {', '.join(_BROWSE_DUMPS)}"
    process_timeout = int(context.browse_timeout_s)
    obscura_timeout = max(1, process_timeout - 1)
    argv = [binary, "fetch", "--dump", dump, "--timeout", str(obscura_timeout)]
    shot_path: Path | None = None
    screenshot = text_or_default(screenshot, "").strip()
    if screenshot:
        # No extension check: obscura always writes PNG whatever the file is
        # called, and js detects images by magic bytes, not by suffix. Rejecting
        # `shot.jpg` would cost a turn and buy nothing.
        shot_path = context.resolve_path(screenshot)
        shot_path.parent.mkdir(parents=True, exist_ok=True)
        argv += ["--screenshot", str(shot_path)]
    if _is_private_ip_literal(url):
        argv.append("--allow-private-network")
    argv.append(url)
    try:
        result = _run_capped(
            argv,
            timeout=process_timeout,
            cwd=str(context.cwd),
            cap=context.max_tool_result_bytes,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: browse timed out after {process_timeout} seconds fetching {_redacted_url(url)}"
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
    if shot_path is not None:
        # obscura's --screenshot short-circuits the dump: it writes the PNG and emits
        # nothing on stdout, and -o writes no file either (measured on obscura 0.2.0).
        # Say so, rather than returning a bare path and letting the caller read the
        # missing page text as "this page is empty".
        if not shot_path.exists():
            return f"ERROR: obscura exited 0 but wrote no screenshot at {shot_path}"
        return (
            f"SCREENSHOT path={shot_path} size={shot_path.stat().st_size} bytes\n"
            "obscura returns either a picture or a dump, never both. "
            "Call browse again without screenshot to read this page's content."
        )
    if dump == "markdown":
        stdout = _absolutize(stdout, url)
    if out_truncated:
        stdout = _fit_truncation_marker(stdout, context.max_tool_result_bytes)
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
                "url": {"type": "string", "description": "Page to read."},
                "dump": {
                    "type": "string",
                    "enum": list(_BROWSE_DUMPS),
                    "default": "markdown",
                    "description": (
                        "What to return: readable markdown, plain text, rendered HTML, the link "
                        "list, the raw response body, the sub-resource URLs, or the cookie jar."
                    ),
                },
                "screenshot": {
                    "type": "string",
                    "description": "Optional .png path to save a picture of the settled page.",
                },
            },
            required=("url",),
        ),
    )
