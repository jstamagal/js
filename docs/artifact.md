# Artifact Mode

Artifact mode curates and queries a local artifact library. It is not the wiki.
It wraps the external `artifact` CLI with native `artifact_*` tools and built-in
prompts.

## Commands

```bash
js --artifact=curate
js --artifact=curate,digest
js --artifact=query "find the last handoff"
js --artifact=lint
```

Modes can be comma-separated. Each mode runs as its own kickoff turn in one
shared session.

## Configuration

```bash
export ARTIFACT_DIR=/srv/artifacts
export ARTIFACT_URL=http://localhost
export ARTIFACT_BIN=artifact
export ARTIFACT_PUSH=auto
```

Defaults:

- `ARTIFACT_DIR`: `/srv/artifacts`
- `ARTIFACT_URL`: `http://localhost`
- `ARTIFACT_BIN`: `artifact`
- `ARTIFACT_PUSH`: `auto` for subprocesses

## Expected Library Shape

Artifact tools read and write through the artifact CLI and library files such
as:

```text
/srv/artifacts/
  manifest.json
  curation.json
  files/
  index.html
```

The built-in prompt explicitly tells the model not to hand-edit
`manifest.json`, `curation.json`, `index.html`, or `files/`.

## Modes

### `curate`

1. Call `artifact_overview()`.
2. Inspect recent, unassigned, or ambiguous artifacts.
3. Build a curation JSON object.
4. Call `artifact_curate(curation_json)`.
5. Report what changed.

### `digest`

1. Call `artifact_overview()`.
2. Summarize recent additions/changes and curation gaps.
3. Write or update an artifact digest page with `artifact_write_page`.

The default digest slug is `artifact-digest` unless the target asks for a dated
digest.

### `query`

1. Call `artifact_overview()`.
2. Use `artifact_search` and `artifact_read`.
3. Answer with stable artifact URLs/slugs.
4. Optionally write a reusable cheatsheet/digest artifact.

### `lint`

Checks for missing assignments, broken refs, duplicate-looking artifacts, stale
uncategorized clusters, and weak titles/tags. Fixes mechanical curation issues
through `artifact_curate`.

## Topic Shelves

The built-in prompt keeps top-level shelves broad and stable:

```text
coding
erotic-coding
devops-systems
cheatsheets
bug-reports
games
music-media
roleplay
erotic-roleplay
emotional-personal
agent-workflows
uncategorized
```

Tags can be specific. Sidebar topic ids should stay broad.

## Tools

- `artifact_overview()`: manifest/curation overview.
- `artifact_search(query, limit)`: search through the artifact CLI.
- `artifact_read(slug)`: metadata plus text preview.
- `artifact_curate(curation_json)`: install curation JSON.
- `artifact_write_page(title, body, slug, tags, desc)`: create/update a page.
- `artifact_ingest(paths, tags, desc)`: ingest raw files.

Subprocesses use `context.fetch_timeout_s` as their timeout.
