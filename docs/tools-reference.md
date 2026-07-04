# Tools Reference

This file describes the public tool surface. The exact model-facing contract
text lives in `js/toolkit/tool_descriptions/*.md`.

## Core Filesystem Tools

### `read`

Reads one file.

Parameters:

- `file_path`: required path.
- `range`: optional `{start_line, end_line}` for text files.
- `show_line_numbers`: default true.

Text output lines are prefixed like:

```text
12ab|line content
```

The prefix is a display anchor, not file content. Do not include it in
`patch`/`multi_patch` strings.

Images return either a vision-disabled text stub or an internal image marker
that the runtime expands for vision models. PDFs use `pdftotext`.

### `write`

Creates or overwrites a whole file.

Parameters:

- `file_path`
- `content`
- `overwrite`

Existing files require `overwrite=true` and a prior `read` in the same process.
The previous state is snapshotted for `undo`.

### `patch`

Performs one exact replacement.

Parameters:

- `file_path`
- `old_string`
- `new_string`
- `replace_all`

Requires a prior `read`. Fails when the old string is absent. Fails on multiple
matches unless `replace_all=true`.

### `multi_patch`

Performs sequential exact replacements in one file.

Parameters:

- `file_path`
- `edits`: list of `{old_string, new_string, replace_all?}`

Requires a prior `read`. The file is snapshotted once before writing.

### `remove`

Trashes or removes a file or directory.

Parameters:

- `path`
- `permanent`: delete directly instead of trashing (default `false`)

Default sends targets to `trash`/`trash-put`; targets over 512 MiB are refused
unless `permanent=true`. Symlinks are removed as symlinks (not followed).
Snapshots the prior file bytes or directory tree for `undo`.

### `undo`

Restores the latest in-process snapshot for a path.

Parameters:

- `path`

Can restore files, created-file nonexistence, and directory snapshots. Snapshots
do not persist across process restarts.

### `fs_search`

Regex search over local files.

Parameters:

- `pattern`
- `path`
- `glob`
- `output_mode`: `files_with_matches`, `content`, `count`
- `-A`, `-B`, `-C`
- `-n`
- `-i`
- `type`
- `head_limit`
- `offset`
- `multiline`

Skips common dependency/cache dirs and binary files.

### `sem_search`

Local intent-ranked search over files.

Parameters:

- `queries`: strings or objects with `query`, `use_case`, `path`, `glob`,
  `limit`

This is not embedding search. It tokenizes query/use-case text and ranks line
and path matches locally.

## Process And Network

### `shell`

Runs a command through the system shell.

Parameters:

- `command`
- `cwd`
- `keep_ansi`
- `env`
- `description`

Unix uses `$SHELL -c`, fallback `/bin/sh -c`. Windows uses `COMSPEC /C`.

Use `cwd` instead of writing `cd ... && ...` in the command.

### `fetch`

Fetches HTTP/HTTPS or `file://` content.

Parameters:

- `url`
- `raw`
- `method`
- `headers`: object of name→value or a list of `"Name: value"` strings
- `body`
- `json_body`
- `save`: path to write the response body to instead of returning it inline

HTML is converted to text unless `raw=true`. Inline output is capped by
`max_tool_result_bytes`; downloads are capped at 32 MiB and return
`SAVED_RESPONSE path=... size=...`.

## Meta Tools

### `todo_write`

Updates the in-process todo map.

Parameters:

- `todos`: list of `{content, status}`

Statuses: `pending`, `in_progress`, `completed`, `cancelled`. Cancelled removes
the item.

### `todo_read`

Reads the in-process todo map.

### `followup`

Stops the current run with a structured follow-up request.

Parameters:

- `question`
- `multiple`
- `option1` through `option5`

Returns `FOLLOWUP_REQUIRED`, which the runtime treats as a stop condition.

### `plan`

Writes a markdown plan under `plans/`.

Parameters:

- `plan_name`
- `version`
- `content`

### `skill`

Loads a local skill document by name from known skill paths.

Parameters:

- `name`

### `task`

Runs one or more subagent tasks.

Parameters:

- `tasks`: required list of strings
- `agent_id`: required worker agent id
- `session_id`: optional child session id

Tasks inside one call run concurrently. Results are returned in input order.

## Wiki Tools

### `wiki_purpose`

Loads vault purpose, section counts, inbox count, and orphan hints.

Parameters:

- `vault`

### `wiki_inbox`

Lists top-level inbox units.

Parameters:

- `vault`

### `wiki_convert`

Converts a source file into text or an Obsidian media embed.

Parameters:

- `path`
- `vault`

Uses text reads, `pdftotext`, `pandoc`, `soffice`, `tesseract`, `ffprobe`, and
`file` depending on extension and available local tools.

### `wiki_write`

Writes or upserts a wiki page with frontmatter.

Parameters:

- `vault`
- `kind`: `source`, `entity`, `concept`, `synthesis`
- `body`
- `slug`
- `title`
- `tags`
- `source`
- `confidence`
- `source_count`
- `overwrite`

Mode gates:

- ingest mode only writes `source`
- synthesize mode refuses `source`

Entity/concept writes include near-match duplicate protection.

### `wiki_search`

Searches a vault through `qmd query`.

Parameters:

- `vault`
- `query`

### `wiki_archive`

Moves a top-level inbox unit to `Clippings/`, or validates and skips movement
when `JS_WIKI_NO_ARCHIVE` is set.

Parameters:

- `vault`
- `unit`

### `wiki_log`

Appends a dated log entry to `log.md`.

Parameters:

- `vault`
- `op`
- `title`
- `note`

### `wiki_finish_ingest`

Atomic ingest closeout: archive, log, and maybe commit.

Parameters:

- `vault`
- `unit`
- `title`
- `note`

If archive fails, logging and commit are skipped.

### `wiki_commit`

Stages and commits a vault if it is a git repo.

Parameters:

- `vault`
- `message`

No-op on non-repo vaults and empty commits.

## Artifact Tools

### `artifact_overview`

Returns manifest/curation overview JSON.

### `artifact_search`

Searches artifacts through the artifact CLI.

Parameters:

- `query`
- `limit`

### `artifact_read`

Reads one artifact's metadata and text preview.

Parameters:

- `slug`

Accepts slug, HTML path, title fragment, or slug fragment when unambiguous.

### `artifact_curate`

Installs curation JSON through the artifact CLI.

Parameters:

- `curation_json`

### `artifact_write_page`

Creates or updates a templated markdown artifact.

Parameters:

- `title`
- `body`
- `slug`
- `tags`
- `desc`

### `artifact_ingest`

Ingests one or more files through the artifact CLI.

Parameters:

- `paths`
- `tags`
- `desc`

## Generated Agent Tools

Prompt directories under repo `prompts/`, global `agents/` in the platform config dir, and
project `.js/agents/` become tools named after the directory. Project scope
wins over global, which wins over repo. Current generated tool names include:

- `defaultagent`
- `autocoder`
- `commit`

Direct agent tools take:

```json
{"tasks":["one or more task strings"]}
```

Whether a model can see a generated tool depends on the active prompt
frontmatter selection.
