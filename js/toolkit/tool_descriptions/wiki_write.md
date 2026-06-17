Create or replace a structured wiki page with frontmatter.

Usage:
- `kind` selects the page family: source, entity, concept, or synthesis.
- Existing pages are protected unless `overwrite=true`; pass a fully merged body when replacing.
- Tags, type, and update date are normalized by the tool.
- Use wikilinks in `body` where the knowledge should connect.

NOTE: under `js --wiki` runs `kind` is mode-gated — ingest mode accepts only kind=source; synthesize mode rejects kind=source (entity/concept/synthesis are built in the synthesize pass).
