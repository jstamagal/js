"""wiki_write: create or UPSERT a wiki page with correct frontmatter."""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..core import ToolContext
from ..sanitize import int_or_default, text_or_default
from ... import colors as C
from .helpers import resolve_vault, slugify, today, KIND_FOLDER, KIND_TYPE, KIND_TAG, vault_lock, wiki_say


# Words that don't carry identity — strip before comparing slugs, so
# "chain-of-draft" and "chain-of-draft-prompting" collapse to {chain, draft}.
_DEDUP_STOP = frozenset({
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "with", "by",
    "method", "methods", "technique", "techniques", "prompting", "approach",
    "approaches", "style", "pattern", "patterns", "model", "models", "system",
    "systems", "spec", "specs", "specification", "specifications", "sheet",
    "json", "md", "pdf", "txt", "page", "pages",
})


def _meaningful_tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[-_\s]+", text.lower())
            if t and len(t) > 2 and t not in _DEDUP_STOP}


def _find_near_matches(folder: Path, target_slug: str) -> list[Path]:
    """Existing pages whose slug shares ≥60% of meaningful tokens with target,
    or whose stem is a substring of target (or vice-versa). Catches
    dayton-dcs165-4 vs dayton-dcs165-4-specs and chain-of-draft vs
    chain-of-draft-prompting BEFORE a duplicate gets written."""
    if not folder.is_dir():
        return []
    target_tokens = _meaningful_tokens(target_slug)
    candidates: list[Path] = []
    for page in folder.glob("*.md"):
        stem = page.stem
        if stem == target_slug:
            continue
        if target_slug in stem or stem in target_slug:
            candidates.append(page)
            continue
        stem_tokens = _meaningful_tokens(stem)
        if not stem_tokens or not target_tokens:
            continue
        overlap = len(target_tokens & stem_tokens)
        smaller = min(len(target_tokens), len(stem_tokens))
        if smaller and overlap / smaller >= 0.6:
            candidates.append(page)
    return candidates


def wiki_write(
    vault: str,
    kind: str,
    body: str,
    slug: str = "",
    title: str = "",
    tags: str = "",
    source: str = "",
    confidence: str = "",
    source_count: int = 1,
    overwrite: bool = False,
    override_dedup: bool = False,
    context: ToolContext = None,
) -> str:
    assert context is not None
    raw_kind = kind
    kind = text_or_default(kind)
    body = text_or_default(body)
    slug = text_or_default(slug)
    title = text_or_default(title)
    tags = text_or_default(tags)
    source = text_or_default(source)
    confidence = text_or_default(confidence)
    source_count = int_or_default(source_count, 1, minimum=1)
    k = kind.strip().lower()
    if k not in KIND_FOLDER:
        return f"ERROR: kind must be source|entity|concept|synthesis (got {raw_kind!r})"
    wiki_mode = getattr(context, "wiki_mode", "") or os.environ.get("JS_WIKI_MODE", "")
    if wiki_mode == "ingest" and k != "source":
        return ("ERROR: ingest mode writes ONLY source pages — entities, concepts, and "
                "synthesis are built in the synthesize pass. Name them in the source body "
                "under '## Candidate entities' / '## Candidate concepts' instead.")
    if wiki_mode == "synthesize" and k == "source":
        return ("ERROR: synthesize mode does not write source pages (no re-ingest). Derive "
                "entity/concept/synthesis pages from the existing source pages.")
    vp = resolve_vault(vault, context)
    if not vp.is_dir():
        return f"ERROR: no vault at {vp}"
    folder = vp / KIND_FOLDER[k]
    folder.mkdir(exist_ok=True)
    s = slugify(slug or title)
    page = folder / f"{s}.md"
    existed = page.exists()
    if existed and not overwrite:
        return (f"EXISTS: {page} already exists. Read it, merge (keep old content, add the new "
                f"link, bump source_count), then call again with overwrite=true. This prevents clobber.")

    # Near-match guard for entity/concept: refuse silently-different slugs that
    # describe the same thing (e.g. -prompting / -method / -specs variants).
    # Synthesis can be near-similar legitimately; source has a 1:1 raw mapping
    # anyway and is allowed.
    if not page.exists() and not overwrite and not override_dedup and k in ("entity", "concept"):
        near = _find_near_matches(folder, s)
        if near:
            listed = "\n  ".join(str(p) for p in near[:5])
            return (f"NEAR-MATCH: existing page(s) likely cover this same {k}:\n  {listed}\n"
                    f"Read the closest one, UPSERT into IT (add the new source link, bump "
                    f"source_count, merge body) with overwrite=true.\n"
                    f"Only pass override_dedup=true if you are CERTAIN this is a genuinely "
                    f"distinct {k} that just shares vocabulary.")

    ptype = KIND_TYPE[k]
    tag_list = [KIND_TAG[ptype]] + [t.strip() for t in tags.split(",") if t.strip()]
    fm = ["---", f"type: {ptype}", "tags: [" + ", ".join(tag_list) + "]"]
    if ptype == "source-summary" and source:
        fm.append(f'source: "{source}"')
    if ptype == "concept" and confidence:
        fm.append(f"confidence: {confidence}")
    if ptype in ("entity", "concept"):
        fm.append(f"source_count: {source_count}")
    fm.append(f"date_updated: {today()}")
    fm.append("---")
    head = f"# {title}" if title else f"# {s}"
    content = "\n".join(fm) + "\n\n" + head + "\n\n" + body.strip() + "\n"
    try:
        with vault_lock(vp, getattr(context, "wiki_vault_lock_timeout_s", 30)):
            page.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"ERROR: {exc}"
    try:
        rel = page.relative_to(vp)
    except ValueError:
        rel = page.name
    wiki_say(f"{C.BR_GREEN}+{ptype}{C.RESET} {rel}", context)
    return f"{'overwrote' if existed else 'wrote'} {page} (type: {ptype})"
