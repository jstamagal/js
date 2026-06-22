# Wiki Mode

Wiki mode maintains local Obsidian-style llm-wikis through deterministic
`wiki_*` tools and built-in mode prompts. The model does the interpretation;
the tools do file movement, page writing, logging, search, conversion, and git
snapshots.

## Commands

```bash
js --wiki=ingest --vault=creative ~/notes/source.md
js --wiki=ingest,synthesize --vault=general ~/papers/paper.pdf
js --wiki=query --vault=creative "what does the wiki know about X?"
js --wiki=lint --vault=general
```

Modes can be comma-separated. Each mode is a separate kickoff turn sharing one
session.

## Vaults

Pass a vault as `--vault <alias|path>`. Aliases are config knobs; the stock
`jsrc` defines:

```text
set wiki.aliases.creative ~/wiki-creative
set wiki.aliases.general ~/wiki-general
```

Any configured alias or filesystem path can be passed. When `--vault` is
omitted, the CLI tries to infer a vault from the target path or current
directory by walking upward and accepting only a `PURPOSE.md` sentinel or a
directory whose name matches `wiki-*`. If nothing resolves, the run stops with
an error. There is no default vault.

Expected vault shape:

```text
<vault>/
  PURPOSE.md
  inbox/
  Clippings/
  sources/
  entities/
  concepts/
  synthesis/
  assets/
  log.md
```

Git is optional. If `<vault>/.git` exists, some tools auto-commit.

## Modes

### `ingest`

Ingest reads one raw unit and writes exactly one `source` page. It does not
write entities, concepts, or synthesis pages. Instead, the source page includes
candidate entity/concept lists for the later synthesize pass.

The `wiki_write` tool enforces this: when `JS_WIKI_MODE=ingest`, it refuses
`kind=entity`, `kind=concept`, and `kind=synthesis`.

Typical flow:

1. `wiki_commit(vault, "pre-ingest snapshot")`
2. `wiki_purpose(vault)`
3. `wiki_inbox(vault)` unless a single file was supplied
4. `wiki_convert(path, vault)`
5. `wiki_write(kind="source", ...)`
6. `wiki_finish_ingest(vault, unit, title, note)`

### `synthesize`

Synthesize owns shared entity/concept pages and cross-source synthesis. It reads
source pages and candidate lists, searches for existing pages, upserts shared
entities/concepts, writes synthesis pages, logs, commits, and updates qmd.

The `wiki_write` tool enforces this: when `JS_WIKI_MODE=synthesize`, it refuses
`kind=source`.

### `query`

Query searches and reads the wiki to answer a question with wiki links. If the
answer is substantial, the model can write it back as a synthesis page.

### `lint`

Lint checks mechanical wiki health: orphans, contradictions, missing refs,
stale claims, missing cross-links, and index drift.

## Single-File Ingest

If `--wiki=ingest` receives a target that is a file, the CLI builds a prompt to
ingest exactly that file.

If the file is a top-level inbox unit, `wiki_finish_ingest` is used so the unit
can be archived or left in place according to mode. If the file is nested inside
`inbox/` or outside the inbox, it is logged but not archived as a top-level
unit.

## Archive And Finish

`wiki_finish_ingest` is the preferred closeout. It calls `wiki_archive` first,
then logs, then maybe commits.

If archiving fails, logging and commit are skipped. This prevents a false
"done" log when the raw inbox unit is still not handled.

When `JS_WIKI_NO_ARCHIVE` is set, `wiki_archive` still validates that the inbox
unit exists, then returns:

```text
archive skipped (leave-in-place): inbox/<unit> kept
```

`wiki_finish_ingest` logs `Left in place: inbox/<unit>` in that case.

## Orphans

`wiki_purpose` scans for source pages that point to an inbox unit that still
exists. This usually means a prior run wrote a source page but failed before
archive. The prompt tells the model to clear those orphans first.

## Page Writing

`wiki_write` adds frontmatter based on kind:

- `source` -> `sources/`, type `source-summary`
- `entity` -> `entities/`, type `entity`
- `concept` -> `concepts/`, type `concept`
- `synthesis` -> `synthesis/`, type `synthesis`

Existing pages require `overwrite=true`. Entity/concept writes also check for
near-match duplicate slugs and return `NEAR-MATCH` unless
`override_dedup=true`.

## Conversion

`wiki_convert` supports:

- text/code direct reads
- JSONL/NDJSON preview
- JSON/CSV/YAML/XML preview
- PDF through `pdftotext`
- office/html formats through `pandoc` or `soffice`
- images through asset copy plus optional `tesseract`
- audio/video through asset copy plus `ffprobe` metadata

Unsupported binary files return a quarantine hint.

## Search And Indexing

`wiki_search` calls:

```bash
qmd query <query> -c <collection>
```

The collection name is the vault directory name.

The synthesize prompt can call:

```bash
qmd update && qmd embed
```

through `shell` after committing.

## Locks

Wiki tools use a vault-level `.wiki.lock` around shared files and git-index
touches. This lets parallel workers write disjoint pages while serializing log
and commit moments.
