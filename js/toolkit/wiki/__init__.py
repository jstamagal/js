"""Deterministic tools used by installed wiki agents."""
from __future__ import annotations

from ..core import Tool
from ..descriptions import load_description
from .convert import wiki_convert
from .ops import wiki_finish_ingest
from .pages import wiki_write


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "wiki_convert",
            load_description("wiki_convert"),
            wiki_convert,
            {
                "path": {"type": "string", "description": "Source file path to convert."},
                "vault": {"type": "string", "description": "Optional vault path for copied media assets."},
            },
            required=("path",),
        ),
        Tool(
            "wiki_write",
            load_description("wiki_write"),
            wiki_write,
            {
                "vault": {"type": "string", "description": "Vault path."},
                "kind": {"type": "string", "description": "Page kind: source, entity, concept, or synthesis."},
                "body": {"type": "string", "description": "Markdown body without frontmatter or H1."},
                "slug": {"type": "string", "description": "Optional filename slug."},
                "title": {"type": "string", "description": "Page title."},
                "tags": {"type": "string", "description": "Comma-separated tags."},
                "source": {"type": "string", "description": "Raw-source path for source pages."},
                "confidence": {"type": "string", "description": "Confidence for concept pages."},
                "source_count": {"type": "integer", "description": "Distinct source count."},
                "overwrite": {"type": "boolean", "description": "Replace an existing exact-slug page."},
                "override_dedup": {"type": "boolean", "description": "Allow a new entity/concept near an existing slug."},
            },
            required=("vault", "kind", "body"),
        ),
        Tool(
            "wiki_finish_ingest",
            load_description("wiki_finish_ingest"),
            wiki_finish_ingest,
            {
                "vault": {"type": "string", "description": "Vault path."},
                "unit": {"type": "string", "description": "Top-level inbox unit to archive."},
                "title": {"type": "string", "description": "Source title for log and commit."},
                "note": {"type": "string", "description": "Summary of pages written."},
            },
            required=("vault", "unit", "title"),
        ),
    )
