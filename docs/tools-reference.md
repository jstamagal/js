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

## Process And Network

### `shell`

Runs a command through the system shell.

Parameters:

- `command`
- `cwd`
- `timeout` (default `300` seconds)
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

## Search And Docs

### `serper_search`

Runs Google-style keyword search through Serper. Use it for current facts,
exact phrases, names, error messages, and recent events.

Parameters:

- `query`: required search terms.
- `num`: result count, default `8`.

Returns an answer box when available, followed by numbered titles, URLs, and
snippets. Requires `SERPER_API_KEY`.

### `tavily_search`

Searches through Tavily and returns extracted page content plus its synthesized
answer when available. Use it for research questions and comparisons where
snippets alone are not enough.

Parameters:

- `query`: required question or search terms.
- `max_results`: result count, default `8`.

Requires `TAVILY_API_KEY`.

### `exa_search`

Runs semantic search through Exa. Use natural-language descriptions when the
right page or concept is known by meaning rather than exact keywords.

Parameters:

- `query`: required description of what to find.
- `num`: result count, default `8`.
- `text_chars`: extracted text per result, default `1500`.

Returns numbered titles, URLs, and page text. Requires `EXA_API_KEY`.

### `docs_search`

Resolves a library through Context7 and fetches current documentation snippets.
Use it before coding against libraries, frameworks, SDKs, or APIs.

Parameters:

- `library`: required library name.
- `topic`: optional documentation area to narrow the result.
- `tokens`: approximate output budget, default `4000`.

Returns the matched Context7 library id and documentation text. An optional
`CONTEXT7_API_KEY` raises rate limits; anonymous access still works.

### `browse`

Reads a JavaScript-rendered page through the `obscura` browser. Use it when a
static HTTP fetch returns an empty shell or when a page is a single-page app.

Parameters:

- `url`: required page URL.
- `dump`: `markdown`, `text`, `html`, or `links`; default `markdown`.

Private and localhost URLs are allowed automatically. This tool cannot render
WebGL or take screenshots. It requires the `obscura` binary on `PATH`.

## Interactive And Visual Tools

### `terminal_session`

Starts and drives commands inside persistent real PTYs. Use it for TUIs,
editors, REPLs, pagers, installers, prompts, and terminal-dependent programs.

Parameters:

- `action`: required; `start`, `send`, `look`, `stop`, or `list`.
- `session`: name, default `main`.
- `command`: shell command required by `start`.
- `keys`: comma-separated named keys or literal text for `send`.
- `cwd`: working directory for `start`.
- `wait_ms`: redraw collection delay, default `700`.
- `cols`, `rows`: terminal dimensions, defaults `64` by `36`.

Named input includes Enter, Tab, Escape, arrows, navigation keys, Ctrl-C/D/L,
and F1 through F12. Results include the rendered screen, cursor, changed-line
count, and process status. Sessions are isolated per `ToolContext`.

### `terminal_snapshot`

Renders an existing `terminal_session` screen to a PNG. It is separate from
terminal control so callers only pay for an image when layout, colour, borders,
wrapping, clipping, or blank-screen diagnosis matters.

Parameters:

- `session`: existing session name, default `main`.
- `output_path`: optional `.png` destination.
- `wait_ms`: pending-redraw collection delay, default `100`.

Default output lands under `terminal-snapshots/` in the session working
directory. The result follows the standard js image contract: `IMAGE_RESULT`
when vision is enabled, otherwise a visual-file metadata stub.

### `browser_probe`

Launches Playwright Chromium with SwiftShader/WebGL support, opens an HTTP(S)
URL or serves a local HTML target, interacts with visible controls, and saves
screenshots into a unique run directory. The Playwright backend is optional
because upstream publishes no musllinux wheels; `just` enables it automatically
on supported platforms.

Parameters:

- `target`: required URL, local HTML file, or local directory.
- `click`: optional `>`-separated regex chain for visible buttons or links.
- `press`: optional keyboard key to hold after clicks.
- `output_dir`: optional parent directory for generated frames.
- `settle_ms`: wait after load and clicks, default `1200`.
- `hold_ms`: key-hold duration, default `1600`.
- `viewport_width`, `viewport_height`: defaults `1280` by `800`.

It captures the largest substantial visible canvas when present, otherwise the
page. Reports frame paths and dimensions, dominant colour share, unique colour
count, changed-pixel percentages, WebGL availability and renderer, console
errors, and uncaught page errors. Use `read` on returned PNG paths for vision.
Use `browse` instead when rendered text or links are sufficient.

## Meta Tools

### `todo_write`

Updates the in-process todo map.

Parameters:

- `todos`: list of `{content, status}`

Statuses: `pending`, `in_progress`, `completed`, `cancelled`. Cancelled removes
the item.

### `todo_read`

Reads the in-process todo map.

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

Installed wiki agents use three deterministic native operations:

### `wiki_convert`

Converts text, structured documents, PDFs, office files, images, and media into
model-readable text or a vault asset embed.

### `wiki_write`

Writes a source, entity, concept, or synthesis page with normalized frontmatter,
exact-slug overwrite protection, near-match dedup guards, and vault locking.

### `wiki_finish_ingest`

Closes one top-level inbox unit in order: archive into `Clippings/`, append
`log.md`, then commit when vault is a git repository.

## Lazy Discovery And MCP Controls

### `tool_discovery`

The canonical discovery and load tool. It is emitted whenever the selected
surface has lazy native tools, skills, or configured MCP servers.

Parameters:

- `query`: words to match in catalog metadata; include `mcp` to connect eligible
  configured servers and fetch their catalogs.
- `kind`: optional `native`, `skill`, or `mcp` filter.
- `source`: optional exact source. For MCP, use `mcp` for all eligible servers or
  the configured server name/normalized name for one server.
- `load`: stable catalog id such as `mcp:files__read_file`.

MCP server tools are model-facing as `<normalized_server>__<normalized_tool>`.
Their full remote schemas are absent until discovery connects the server and a
later `load` call loads that exact catalog id. The schema is emitted on the next
model call, never retroactively in the batch which loaded it.

The canonical resource and prompt controls are:

- `mcp_resource_list(server)`
- `mcp_resource_templates(server)`
- `mcp_resource_read(server, uri)`
- `mcp_resource_subscribe(server, uri)`
- `mcp_resource_unsubscribe(server, uri)`
- `mcp_prompt_list(server)`
- `mcp_prompt_get(server, name, arguments?)`

Controls also load through `tool_discovery`, for example
`{"load":"mcp:mcp_resource_read"}`. Their `server` argument is the exact
configured server name returned by discovery. Server tools, controls, and loaded
schemas last only for the current turn; persistent MCP connections may be reused
by a session host, but a later turn begins with only `tool_discovery` again.

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
