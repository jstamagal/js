"""Artifact toolkit — native tools for KING's artifact library."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..sanitize import int_or_default, text_or_default
from ..core import Tool, ToolContext
from ..descriptions import load_description
from .prompts import build_artifact_system as build_artifact_system

ARTIFACT_DIR = Path(os.environ.get("ARTIFACT_DIR", "/srv/artifacts"))
BASE_URL = os.environ.get("ARTIFACT_URL", "http://localhost")
ARTIFACT_BIN = os.environ.get("ARTIFACT_BIN", "artifact")


def _artifact_dir(context: ToolContext | None = None) -> Path:
    if context is not None and getattr(context, "artifact_dir", None):
        return Path(context.artifact_dir)
    return Path(os.environ.get("ARTIFACT_DIR", str(ARTIFACT_DIR)))


def _base_url(context: ToolContext | None = None) -> str:
    if context is not None and getattr(context, "artifact_url", None):
        return str(context.artifact_url).rstrip("/")
    return os.environ.get("ARTIFACT_URL", BASE_URL).rstrip("/")


def _artifact_bin(context: ToolContext | None = None) -> str:
    if context is not None and getattr(context, "artifact_bin", None):
        return str(context.artifact_bin)
    return os.environ.get("ARTIFACT_BIN", ARTIFACT_BIN)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _truncate(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"



def _entry_source(entry: dict[str, Any], root: Path) -> Path:
    if entry.get("asset"):
        return root / entry["asset"]
    if entry.get("src"):
        return root / entry["src"]
    return root / entry.get("html", "")


def _read_text(path: Path, limit: int) -> str:
    try:
        data = path.read_bytes()[:limit]
    except OSError:
        return ""
    # A byte-limit slice can cut mid-codepoint; decode leniently so a large
    # non-ASCII artifact still yields a preview instead of dropping the whole body.
    return data.decode("utf-8", errors="replace")


def _entry_text(entry: dict[str, Any], root: Path, limit: int) -> str:
    kind = entry.get("kind", "page")
    if kind not in {"page", "html", "json", "markdown", "text"}:
        return ""
    return _read_text(_entry_source(entry, root), limit)


def _run_artifact(args: list[str], context: ToolContext) -> str:
    env = {**os.environ, "ARTIFACT_PUSH": os.environ.get("ARTIFACT_PUSH", "auto")}
    try:
        result = subprocess.run(
            [_artifact_bin(context), *args],
            cwd=str(context.cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=context.fetch_timeout_s,
        )
    except FileNotFoundError:
        return f"ERROR: artifact command not found: {_artifact_bin(context)}"
    except subprocess.TimeoutExpired:
        return "ERROR: artifact command timed out"
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        return f"ERROR: artifact {' '.join(args)} failed\n{err or out}"
    return out or err or "ok"


def artifact_overview(context: ToolContext | None = None) -> str:
    assert context is not None
    root = _artifact_dir(context)
    manifest = _load_json(root / "manifest.json", [])
    curation = _load_json(root / "curation.json", {"topics": [], "assignments": {}, "refs": {}})
    assignments = curation.get("assignments", {}) if isinstance(curation, dict) else {}
    refs = curation.get("refs", {}) if isinstance(curation, dict) else {}
    topic_counts: dict[str, int] = {}
    unassigned = []
    for entry in manifest:
        topics = assignments.get(entry.get("slug"), [])
        if not topics:
            unassigned.append(entry.get("slug"))
        for topic in topics:
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
    recent = sorted(manifest, key=lambda e: e.get("created", ""), reverse=True)[:25]
    payload = {
        "root": str(root),
        "base_url": _base_url(context),
        "count": len(manifest),
        "topic_counts": topic_counts,
        "unassigned": unassigned[:100],
        "refs_count": len(refs),
        "recent": [
            {
                "slug": e.get("slug"),
                "title": e.get("title"),
                "kind": e.get("kind", "page"),
                "tags": e.get("tags", []),
                "desc": e.get("desc", ""),
                "url": f"{_base_url(context)}/{e.get('html')}",
            }
            for e in recent
        ],
    }
    return json.dumps(payload, indent=2)


def artifact_search(query: str, limit: int = 20, context: ToolContext | None = None) -> str:
    assert context is not None
    safe_limit = int_or_default(limit, 20, minimum=1)
    return _run_artifact(["search", text_or_default(query), "--limit", str(safe_limit)], context)


def artifact_read(slug: str, context: ToolContext | None = None) -> str:
    assert context is not None
    slug = text_or_default(slug)
    if not slug:
        return "ERROR: no artifact matches ''"
    root = _artifact_dir(context)
    manifest = _load_json(root / "manifest.json", [])
    matches = [e for e in manifest if e.get("slug") == slug or e.get("html") == slug]
    if not matches:
        needle = slug.lower()
        matches = [e for e in manifest if needle in e.get("slug", "").lower() or needle in e.get("title", "").lower()]
    if not matches:
        return f"ERROR: no artifact matches {slug!r}"
    if len(matches) > 1:
        return "ERROR: ambiguous artifact: " + ", ".join(e.get("slug", "?") for e in matches)
    entry = dict(matches[0])
    entry["url"] = f"{_base_url(context)}/{entry.get('html')}"
    if entry.get("asset"):
        entry["asset_url"] = f"{_base_url(context)}/{entry['asset']}"
    text = _entry_text(entry, root, context.max_tool_result_bytes // 2)
    if text:
        entry["text_preview"] = _truncate(text, context.max_tool_result_bytes // 2)
    return json.dumps(entry, indent=2)


def artifact_curate(curation_json: str, context: ToolContext | None = None) -> str:
    assert context is not None
    curation_json = text_or_default(curation_json)
    try:
        json.loads(curation_json)
    except json.JSONDecodeError as exc:
        return f"ERROR: invalid curation JSON: {exc}"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        fh.write(curation_json)
        path = fh.name
    try:
        return _run_artifact(["curate", path], context)
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass


def artifact_write_page(
    title: str,
    body: str,
    slug: str = "",
    tags: str = "",
    desc: str = "",
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    title = text_or_default(title)
    body = text_or_default(body)
    slug = text_or_default(slug)
    tags = text_or_default(tags)
    desc = text_or_default(desc)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(body)
        path = fh.name
    try:
        root = _artifact_dir(context)
        manifest = _load_json(root / "manifest.json", [])
        exists = bool(slug and any(e.get("slug") == slug or e.get("html") == slug for e in manifest))
        if exists:
            args = ["update", slug, path, "--title", title]
        else:
            args = ["new", path, "--title", title]
        if tags:
            args += ["--tags", tags]
        if desc:
            args += ["--desc", desc]
        return _run_artifact(args, context)
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass


def artifact_ingest(paths: str, tags: str = "", desc: str = "", context: ToolContext | None = None) -> str:
    assert context is not None
    paths = text_or_default(paths)
    tags = text_or_default(tags)
    desc = text_or_default(desc)
    resolved = [str(context.resolve_path(p)) for p in paths.split() if p.strip()]
    if not resolved:
        return "ERROR: no paths supplied"
    args = ["ingest", *resolved]
    if tags:
        args += ["--tags", tags]
    if desc:
        args += ["--desc", desc]
    return _run_artifact(args, context)


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "artifact_overview",
            load_description("artifact_overview"),
            artifact_overview,
            {},
        ),
        Tool(
            "artifact_search",
            load_description("artifact_search"),
            artifact_search,
            {
                "query": {"type": "string", "description": "Search terms matched against artifact metadata and text-like content."},
                "limit": {"type": "integer", "default": 20, "description": "Maximum number of search results to return."},
            },
            required=("query",),
        ),
        Tool(
            "artifact_read",
            load_description("artifact_read"),
            artifact_read,
            {"slug": {"type": "string", "description": "Artifact slug, HTML path, title fragment, or slug fragment."}},
            required=("slug",),
        ),
        Tool(
            "artifact_curate",
            load_description("artifact_curate"),
            artifact_curate,
            {"curation_json": {"type": "string", "description": "Complete curation JSON containing topics, assignments, and refs."}},
            required=("curation_json",),
        ),
        Tool(
            "artifact_write_page",
            load_description("artifact_write_page"),
            artifact_write_page,
            {
                "title": {"type": "string", "description": "Artifact page title."},
                "body": {"type": "string", "description": "Markdown body to render into the artifact house style."},
                "slug": {"type": "string", "description": "Existing slug to update; leave blank to create a new page."},
                "tags": {"type": "string", "description": "Comma-separated tag list for search and dashboard filtering."},
                "desc": {"type": "string", "description": "One-line description shown in artifact indexes."},
            },
            required=("title", "body"),
        ),
        Tool(
            "artifact_ingest",
            load_description("artifact_ingest"),
            artifact_ingest,
            {
                "paths": {"type": "string", "description": "Whitespace- or newline-separated file paths to ingest."},
                "tags": {"type": "string", "description": "Comma-separated tags to apply to ingested artifacts."},
                "desc": {"type": "string", "description": "Shared one-line description for the ingested artifacts."},
            },
            required=("paths",),
        ),
    )
