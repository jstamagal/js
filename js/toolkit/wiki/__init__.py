"""Wiki toolkit — native tools for ingesting and synthesizing an llm-wiki.

Plumbing only: these tools do the deterministic file work (convert, write pages,
search, archive, log). The MODEL does the reasoning (what is an entity vs concept,
what the synthesis says). Each vault's PURPOSE.md carries its domain lens — call
wiki_purpose first to load it.
"""
from __future__ import annotations

from ..core import Tool
from ..descriptions import load_description
from .convert import wiki_convert
from .pages import wiki_write
from .ops import wiki_purpose, wiki_inbox, wiki_search, wiki_archive, wiki_log, wiki_finish_ingest, wiki_commit
from .helpers import infer_vault as infer_vault
from .prompts import build_wiki_system as build_wiki_system


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "wiki_purpose",
            load_description("wiki_purpose"),
            wiki_purpose,
            {"vault": {"type": "string", "description": "Vault alias ('creative' or 'general') or a vault path."}},
            required=("vault",),
        ),
        Tool(
            "wiki_inbox",
            load_description("wiki_inbox"),
            wiki_inbox,
            {"vault": {"type": "string", "description": "Vault alias or path whose inbox should be listed."}},
            required=("vault",),
        ),
        Tool(
            "wiki_convert",
            load_description("wiki_convert"),
            wiki_convert,
            {
                "path": {"type": "string", "description": "Source file path to convert for ingestion."},
                "vault": {"type": "string", "description": "Optional vault alias or path used when media must be copied into assets."},
            },
            required=("path",),
        ),
        Tool(
            "wiki_write",
            load_description("wiki_write"),
            wiki_write,
            {
                "vault": {"type": "string", "description": "Vault alias or path to write into."},
                "kind": {"type": "string", "enum": ["source", "entity", "concept", "synthesis"], "description": "Page family; determines folder, type, and base tag."},
                "body": {"type": "string", "description": "Markdown body without frontmatter."},
                "slug": {"type": "string", "description": "Optional URL/file slug; generated from title if omitted."},
                "title": {"type": "string", "description": "Human-readable page title."},
                "tags": {"type": "string", "description": "Comma-separated extra tags without leading #."},
                "source": {"type": "string", "description": "Source citation or origin path to store in frontmatter."},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"], "description": "Confidence label for extracted knowledge."},
                "source_count": {"type": "integer", "default": 1, "description": "Number of sources supporting the page."},
                "overwrite": {"type": "boolean", "default": False, "description": "Allow replacing an existing page with a fully merged body."},
            },
            required=("vault", "kind", "body"),
        ),
        Tool(
            "wiki_search",
            load_description("wiki_search"),
            wiki_search,
            {
                "vault": {"type": "string", "description": "Vault alias or path to search."},
                "query": {"type": "string", "description": "Search query for qmd hybrid retrieval."},
            },
            required=("vault", "query"),
        ),
        Tool(
            "wiki_archive",
            load_description("wiki_archive"),
            wiki_archive,
            {
                "vault": {"type": "string", "description": "Vault alias or path containing the inbox unit."},
                "unit": {"type": "string", "description": "Top-level inbox file or folder name to move into Clippings/."},
            },
            required=("vault", "unit"),
        ),
        Tool(
            "wiki_log",
            load_description("wiki_log"),
            wiki_log,
            {
                "vault": {"type": "string", "description": "Vault alias or path whose log.md should be appended."},
                "op": {"type": "string", "enum": ["ingest", "synth", "lint", "skip"], "description": "Operation category for the log heading."},
                "title": {"type": "string", "description": "Log entry title."},
                "note": {"type": "string", "description": "Optional concise note for the log body."},
            },
            required=("vault", "op", "title"),
        ),
        Tool(
            "wiki_finish_ingest",
            load_description("wiki_finish_ingest"),
            wiki_finish_ingest,
            {
                "vault": {"type": "string", "description": "Vault alias or path containing the completed inbox unit."},
                "unit": {"type": "string", "description": "Top-level inbox unit to archive."},
                "title": {"type": "string", "description": "Title for the ingest log and commit message."},
                "note": {"type": "string", "description": "Optional ingest summary for log.md."},
            },
            required=("vault", "unit", "title"),
        ),
        Tool(
            "wiki_commit",
            load_description("wiki_commit"),
            wiki_commit,
            {
                "vault": {"type": "string", "description": "Vault alias or path to commit if it is a git repo."},
                "message": {"type": "string", "description": "Commit message for the vault snapshot."},
            },
            required=("vault", "message"),
        ),
    )
