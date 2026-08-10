from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from js.capped_process import CappedProcessResult, truncation_marker
from js.toolkit import search
from js.toolkit.core import ToolContext


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def _responses(monkeypatch: pytest.MonkeyPatch, *bodies: bytes) -> list[tuple[Any, float]]:
    pending = iter(bodies)
    calls: list[tuple[Any, float]] = []

    def fake_urlopen(request: Any, timeout: float) -> _Response:
        calls.append((request, timeout))
        return _Response(next(pending))

    monkeypatch.setattr(search.urllib.request, "urlopen", fake_urlopen)
    return calls


def _browse_stub(monkeypatch: pytest.MonkeyPatch, run: Callable[..., CappedProcessResult]) -> None:
    monkeypatch.setattr(search, "resolve_binary", lambda _name: "/test/bin/obscura")
    monkeypatch.setattr(search, "_run_capped", run)


@pytest.mark.parametrize(
    ("handler", "key_name", "host"),
    [
        (search.serper_search, "SERPER_API_KEY", "google.serper.dev"),
        (search.tavily_search, "TAVILY_API_KEY", "api.tavily.com"),
        (search.exa_search, "EXA_API_KEY", "api.exa.ai"),
        (search.docs_search, None, "context7.com"),
    ],
)
@pytest.mark.parametrize(
    ("body", "kind"),
    [(b'"quota exceeded"', "string"), (b"null", "null"), (b"[]", "array")],
)
def test_provider_root_response_must_be_an_object(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handler: Callable[..., str],
    key_name: str | None,
    host: str,
    body: bytes,
    kind: str,
) -> None:
    if key_name is not None:
        monkeypatch.setenv(key_name, "test-key")
    _responses(monkeypatch, body)

    actual = handler("test query", context=ToolContext(cwd=tmp_path))

    assert actual == f"ERROR: expected a JSON object from {host}, got {kind}"


@pytest.mark.parametrize(
    ("handler", "key_name", "body"),
    [
        (search.serper_search, "SERPER_API_KEY", b'{"organic":["bad hit"]}'),
        (search.tavily_search, "TAVILY_API_KEY", b'{"results":["bad hit"]}'),
        (search.exa_search, "EXA_API_KEY", b'{"results":["bad hit"]}'),
        (search.docs_search, None, b'{"results":["bad hit"]}'),
    ],
)
def test_provider_result_entries_must_be_objects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handler: Callable[..., str],
    key_name: str | None,
    body: bytes,
) -> None:
    if key_name is not None:
        monkeypatch.setenv(key_name, "test-key")
    _responses(monkeypatch, body)

    actual = handler("test query", context=ToolContext(cwd=tmp_path))

    assert actual == "ERROR: expected search result 1 to be an object, got string"


def test_provider_results_field_must_be_an_array(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    _responses(monkeypatch, b'{"organic":"bad results"}')

    actual = search.serper_search("test query", context=ToolContext(cwd=tmp_path))

    assert actual == "ERROR: expected search results to be an array, got string"


def test_serper_answer_box_must_be_an_object(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    _responses(monkeypatch, b'{"answerBox":[],"organic":[]}')

    actual = search.serper_search("test query", context=ToolContext(cwd=tmp_path))

    assert actual == "ERROR: expected answerBox to be an object, got array"


@pytest.mark.parametrize(
    ("handler", "key_name"),
    [
        (search.serper_search, "SERPER_API_KEY"),
        (search.tavily_search, "TAVILY_API_KEY"),
        (search.exa_search, "EXA_API_KEY"),
        (search.docs_search, None),
    ],
)
def test_required_search_text_rejects_whitespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handler: Callable[..., str],
    key_name: str | None,
) -> None:
    if key_name is not None:
        monkeypatch.setenv(key_name, "test-key")

    def unexpected_request(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("whitespace-only input reached the HTTP client")

    monkeypatch.setattr(search.urllib.request, "urlopen", unexpected_request)

    actual = handler(" \t\n ", context=ToolContext(cwd=tmp_path))

    noun = "library" if handler is search.docs_search else "query"
    assert actual == f"ERROR: {noun} is required"


@pytest.mark.parametrize(
    ("handler", "key_name", "argument", "value", "expected"),
    [
        (search.serper_search, "SERPER_API_KEY", "num", 0, 1),
        (search.serper_search, "SERPER_API_KEY", "num", 100_000, 100),
        (search.serper_search, "SERPER_API_KEY", "num", 2.9, 8),
        (search.tavily_search, "TAVILY_API_KEY", "max_results", 0, 1),
        (search.tavily_search, "TAVILY_API_KEY", "max_results", 100_000, 20),
        (search.tavily_search, "TAVILY_API_KEY", "max_results", 2.9, 8),
        (search.exa_search, "EXA_API_KEY", "num", 0, 1),
        (search.exa_search, "EXA_API_KEY", "num", 100_000, 100),
        (search.exa_search, "EXA_API_KEY", "num", 2.9, 8),
    ],
)
def test_search_result_count_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handler: Callable[..., str],
    key_name: str,
    argument: str,
    value: object,
    expected: int,
) -> None:
    monkeypatch.setenv(key_name, "test-key")
    response = b'{"organic":[]}' if handler is search.serper_search else b'{"results":[]}'
    calls = _responses(monkeypatch, response)

    actual = handler("test query", **{argument: value}, context=ToolContext(cwd=tmp_path))

    assert actual == "no results"
    payload = json.loads(calls[0][0].data)
    payload_key = "num" if handler is search.serper_search else "max_results" if handler is search.tavily_search else "numResults"
    assert payload[payload_key] == expected


@pytest.mark.parametrize("text_chars", [0, 50, "8"])
def test_exa_text_budget_clamps_to_minimum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    text_chars: object,
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    calls = _responses(monkeypatch, b'{"results":[]}')

    actual = search.exa_search("test query", text_chars=text_chars, context=ToolContext(cwd=tmp_path))

    assert actual == "no results"
    payload = json.loads(calls[0][0].data)
    assert payload["contents"]["text"]["maxCharacters"] == 100


@pytest.mark.parametrize(("tokens", "expected"), [(100, "500"), (499, "500"), (500, "500")])
def test_docs_token_budget_clamps_to_minimum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tokens: int,
    expected: str,
) -> None:
    calls = _responses(monkeypatch, b'{"results":[{"id":"/tiangolo/fastapi"}]}', b"routing docs")

    actual = search.docs_search("fastapi", topic="routing", tokens=tokens, context=ToolContext(cwd=tmp_path))

    assert actual == "[context7 /tiangolo/fastapi, topic: routing]\nrouting docs"
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[1][0].full_url).query)
    assert query["tokens"] == [expected]


def test_docs_empty_body_is_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _responses(monkeypatch, b'{"results":[{"id":"/tiangolo/fastapi"}]}', b" \n\t")

    actual = search.docs_search("fastapi", topic=" routing ", context=ToolContext(cwd=tmp_path))

    assert actual == "ERROR: context7 returned empty docs for /tiangolo/fastapi, topic: routing"


def test_http_error_redacts_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key = "sk-live-ABC123"
    monkeypatch.setenv("SERPER_API_KEY", key)

    def fake_urlopen(request: Any, timeout: float) -> Any:
        body = io.BytesIO(b'{"message":"Unauthorized: bad api key sk-live-ABC123"}')
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, body)

    monkeypatch.setattr(search.urllib.request, "urlopen", fake_urlopen)

    actual = search.serper_search("test query", context=ToolContext(cwd=tmp_path))

    assert actual == 'ERROR: HTTP 401 from google.serper.dev: {"message":"Unauthorized: bad api key [REDACTED]"}'


@pytest.mark.parametrize(
    "url",
    [
        "http://172.17.0.1:8733/",
        "http://172.20.10.5/",
        "http://172.31.255.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fd00::1]/",
    ],
)
def test_browse_allows_private_ip_literals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str) -> None:
    def run(argv: list[str], **_kwargs: object) -> CappedProcessResult:
        output = b"private literal allowed" if "--allow-private-network" in argv else b"private literal blocked"
        return CappedProcessResult(0, output, b"")

    _browse_stub(monkeypatch, run)

    actual = search.browse(url, context=ToolContext(cwd=tmp_path))

    assert actual == "private literal allowed"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost.127.0.0.1.nip.io:8733/",
        "http://safe.127.0.0.1.nip.io:8733/",
        "http://myserver.local/",
        "http://localhost:8733/",
    ],
)
def test_browse_retains_ssrf_guard_for_hostnames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str) -> None:
    def run(argv: list[str], **_kwargs: object) -> CappedProcessResult:
        output = b"public hostname bypassed guard" if "--allow-private-network" in argv else b"public hostname retained guard"
        return CappedProcessResult(0, output, b"")

    _browse_stub(monkeypatch, run)

    actual = search.browse(url, context=ToolContext(cwd=tmp_path))

    assert actual == "public hostname retained guard"


def test_browse_timeout_reports_redacted_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def run(argv: list[str], **_kwargs: object) -> CappedProcessResult:
        raise subprocess.TimeoutExpired(argv, 15)

    _browse_stub(monkeypatch, run)
    url = "http://admin:hunter2@127.0.0.1:8733/slow?signature=secret-token"

    actual = search.browse(url, context=ToolContext(cwd=tmp_path, fetch_timeout_s=15))

    assert actual == "ERROR: browse timed out after 15 seconds fetching http://127.0.0.1/slow"


def test_browse_sets_obscura_timeout_below_process_backstop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def run(argv: list[str], **kwargs: object) -> CappedProcessResult:
        captured.update(argv=argv, **kwargs)
        return CappedProcessResult(0, b"partial page", b"")

    _browse_stub(monkeypatch, run)

    actual = search.browse("https://example.com", context=ToolContext(cwd=tmp_path, fetch_timeout_s=15))

    assert actual == "partial page"
    assert captured == {
        "argv": [
            "/test/bin/obscura",
            "fetch",
            "--dump",
            "markdown",
            "--timeout",
            "14",
            "https://example.com",
        ],
        "timeout": 15,
        "cwd": str(tmp_path),
        "cap": 256 * 1024,
    }


def test_browse_truncation_marker_fits_result_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cap = 128

    def run(_argv: list[str], **_kwargs: object) -> CappedProcessResult:
        return CappedProcessResult(0, b"x" * cap, b"", stdout_truncated=True)

    _browse_stub(monkeypatch, run)

    actual = search.browse("https://example.com", context=ToolContext(cwd=tmp_path, max_tool_result_bytes=cap))

    marker = truncation_marker(cap, "limits.max_tool_result_bytes")
    expected = "x" * (cap - len(marker.encode()) - 1) + "\n" + marker
    assert actual == expected
    assert len(actual.encode()) == cap


@pytest.mark.parametrize("dump", [" markdown ", "MARKDOWN", " Markdown\n"])
def test_browse_normalizes_dump(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dump: str) -> None:
    def run(argv: list[str], **_kwargs: object) -> CappedProcessResult:
        output = b"normalized markdown" if argv[3] == "markdown" else b"unnormalized dump"
        return CappedProcessResult(0, output, b"")

    _browse_stub(monkeypatch, run)

    actual = search.browse("https://example.com", dump=dump, context=ToolContext(cwd=tmp_path))

    assert actual == "normalized markdown"
