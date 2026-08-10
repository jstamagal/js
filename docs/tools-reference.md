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

The prefix is a display anchor, not file content. Do not include it in `patch`
strings.

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

Performs exact replacements in one file, one or many.

Parameters:

- `file_path`
- `old_string`
- `new_string`
- `replace_all`
- `edits`: list of `{old_string, new_string, replace_all?}`, used instead of
  `old_string`/`new_string`

Requires a prior `read`. Each edit fails when its old string is absent, on
multiple matches without `replace_all=true`, and when `old_string` equals
`new_string`.

`edits` applies its replacements in order, each seeing the previous one's
result, and is atomic: any failure writes nothing and the error names the
offending edit by position. Passing `edits` together with the scalar
`old_string`/`new_string`/`replace_all` arguments is an error. A successful call
writes once and snapshots once, so one `undo` reverts the whole call.

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

### `ast_search`

Structural search and optional rewrite over parsed source code, backed by
ast-grep 0.45.1 — the managed `js/tools/ast-grep` installed by `just install`,
falling back to `ast-grep` on PATH before that.

Parameters:

- `pattern`: required ast-grep pattern; `$NAME` captures one node and
  `$$$ARGS` captures zero or more nodes.
- `path`: file or directory, default current working directory.
- `lang`: optional parser override from ast-grep's supported language enum.
- `rewrite`: optional structural replacement; dry-run diff by default.
- `apply`: set true to apply a supplied rewrite, default false.
- `max_results`: maximum matches returned or rewritten, default `100`.

Search output uses absolute path headings and the same anchored source lines as
`read`. Applying a rewrite snapshots every affected file for `undo`, clears the
shared search cache, and refuses match sets larger than `max_results`.

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
`max_tool_result_bytes`. A download streams to disk and returns
`SAVED_RESPONSE path=... size=...`; it is bounded by
`limits.max_download_bytes`, which defaults to `0`, meaning unlimited.

Saved GET responses, binary responses, and bodies over the inline result budget
are transferred through the system aria2c with segmentation, retry, resume,
and atomic destination replacement. Missing aria2c emits a runtime warning
before urllib handles the transfer.

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
WebGL or take screenshots. `just install` downloads the pinned `obscura`
release asset into `js/tools` — the stealth build, which carries TLS
impersonation alongside the browser fingerprint — together with the
`obscura-worker` binary it spawns. Before that install, the tool falls back to
PATH.

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

## Persistent Kernel Tools

`kernel` and `toolbox` are the other side of js from the curated tool surface:
instead of handing an agent forty fixed tools, hand it a stateful Python REPL and
let it build its own. They do not replace the hardened tools, do not wrap them,
and do not import their rails. The shipped `twotool` agent
(`js --agent twotool`) is this mode: `kernel`, `toolbox`, `shell`, nothing else.

Both need `jupyter_client` and `ipykernel`, which are base dependencies —
`just sync` installs them. Without them the tools return one ERROR naming the
missing package instead of a traceback, and the rest of js is unaffected.

### `kernel`

Executes Python in a persistent IPython kernel. One kernel per ToolContext, held
for the life of the session, so a function defined in call 3 is callable in call
30.

Parameters:

- `code`: the Python to run. Empty `code` reports the namespace and runs nothing.
- `timeout`: seconds, default 120.
- `restart`: kill and restart, destroying every definition.
- `verbosity`: `quiet`, `normal`, or `verbose` for this call's terminal render.

Every result carries a `NAMESPACE` line naming the callables live in the kernel
*right now*, re-derived from the kernel on every call rather than remembered.
That is what makes the tool survive compaction: the transcript that defined
`parse_log` may be gone, the listing is not. New definitions also get a `DEFINED`
line, deletions a `GONE` line.

A cell that exceeds `timeout` is interrupted with SIGINT, exactly like Ctrl-C in
a notebook — never restarted. The cell dies, the namespace lives. A kernel that
actually dies is reported as such, naming the cell, instead of blocking until the
deadline. Image output (matplotlib and friends) is written under `.js/kernel/`
and reported as `IMAGE <path>`; the kernel process's own stderr goes to
`.js/kernel/kernel.log`, never the terminal. The result string is capped by
`limits.max_tool_result_bytes` with the standard truncation marker.

This tool has no opinion about persistence. It does not save, load, or version
anything, and it does not import `toolbox`.

### `toolbox`

Tools that outlive the session, so a tool written by a weak local model on Monday
is loadable, refinable, and re-saveable by a stronger model on Tuesday.

Parameters:

- `action` (required): `list`, `load`, `save`, `history`, `restore`.
- `name`: the tool name — a plain Python identifier matching the definition.
- `note`: for `save`, what changed and why. This is the message the next model
  reads.
- `scope`: `global` (platform config dir `toolbox/`) or `project`
  (`.js/toolbox/`). Project shadows global, mirroring agent precedence.
- `revision`: for `restore`.
- `source`: an explicit definition, for saving without a live kernel.
- `verbosity`: as for `kernel`.

Each tool is one file with a machine-readable provenance header:

```text
# js-toolbox: {"history":[{"date":"2026-08-10","model":"qwen","note":"first cut","revision":1}],"name":"summarise","revision":1}
```

`save` reads the definition out of the live kernel, resolves the free names it
references against the kernel namespace, and prepends the `import` lines it needs
so the saved file stands alone. Names it cannot resolve to a module (a sibling
function, a module-level constant) come back as a `WARNING` naming them — a tool
saved without them would `NameError` on the next session's `load`.

`save` never overwrites: revision N is archived to `.history/<name>.rN.py` and
N+1 is written. `restore` rolls back by writing the old body as a *new* revision,
so history is append-only and nothing is ever lost.

`load` execs every healthy tool file into the kernel, each inside its own
try/except. One broken tool file costs that tool and reports it by name; the rest
of the box loads.

### Rendering and verbosity

`kernel` and `toolbox` render what happened to *your terminal* on stderr with
rich — code, output, elapsed, the namespace, save/load activity. stderr so
`js -p '...' | jq` still works.

- `quiet` — errors and interrupts only.
- `normal` — code, merged output, elapsed, namespace, defined/gone.
- `verbose` — stdout/stderr/display split apart, plus kernel lifecycle events.

Set the baseline with `set kernel.verbosity <level>` (or `JS_KERNEL_VERBOSITY`);
override one call with the tool's `verbosity` parameter. `set
kernel.render_max_lines <n>` (default 24) caps each rendered section so a
4000-line cell cannot scroll the screen away; the hidden count is always shown.

Verbosity governs the terminal render only. The model always receives the full,
identically-shaped result — a display knob that could silently delete the
`NAMESPACE` line would break the property the whole tool rests on.

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

- `query`: words to match in catalog metadata.
- `kind`: optional `native`, `skill`, or `mcp` filter. Use `mcp` to connect all
  eligible configured servers and fetch their catalogs.
- `source`: optional exact source. A configured MCP server name or normalized
  name connects only that server.
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
- `twotool`

Direct agent tools take:

```json
{"tasks":["one or more task strings"]}
```

Whether a model can see a generated tool depends on the active prompt
frontmatter selection.
