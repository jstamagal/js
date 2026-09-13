# Technical Guide

This is the architecture guide for contributors and agents working on `js`.

## Package Entry Points

`pyproject.toml` exposes:

```toml
[project.scripts]
js = "js.cli:main"
```

`js/__main__.py` delegates to the same CLI path.

## Runtime Shape

One normal prompt run follows this path:

1. `js.cli.main()` parses flags.
2. `_from_env()` builds `Config`.
3. `js.persona.load_prompt_spec()` loads the selected agent from repo
   `prompts/`, global `agents/` in the platform config dir, and project `.js/agents/`.
4. `ToolRegistry.select()` filters the default registry by prompt selectors.
5. Existing session messages are loaded through `js.memory.load_messages()`.
6. The new user message is appended to the in-memory list.
7. `js.runtime.run_turn()` loops over model calls and tool calls.
8. The CLI persists new messages after a final assistant response.

The REPL uses the same runtime but keeps the message list in process and
persists each completed turn.

## Module Responsibilities

| Module | Responsibility |
| `js/cli.py` | argument parser, REPL, prompt/pipe mode, commit orchestration |
| `js/config.py` | environment parsing, session reservation, model/provider caps, vision heuristic |
| `js/model_client.py` | single import boundary for the Vercel AI Python SDK |
| `js/runtime.py` | streaming loop, tool-call aggregation, dispatch, provider quirks |
| `js/memory.py` | locked JSONL persistence and loader control marks |
| `js/persona.py` | prompt-directory concatenation and `tools:` frontmatter |
| `js/tools.py` | compatibility import of the default registry/context |
| `js/toolkit/core.py` | `Tool`, `ToolContext`, argument coercion, handler invocation |
| `js/toolkit/registry.py` | default registry assembly and selector matching |
| `js/toolkit/fs.py` | file read/write/search/edit/delete/undo tools |
| `js/toolkit/process_net.py` | shell and fetch tools |
| `js/toolkit/meta.py` | todo/plan/skill/task and generated agent tools |
| `js/toolkit/wiki/` | deterministic tools for installed wiki agents |

## Prompt Loading

Prompt files are sorted by filename and concatenated with blank lines. Only the
first zero file (`00.md`, `00-*.md`, or `00_*.md`) is parsed for YAML
frontmatter.

Example:

```markdown
---
tools:
  - read
  - fs_search
  - todo_*
  - task
---

System prompt.
```

`tools` must be a list of strings. Selectors can be exact names, glob patterns,
or `*`. No tools selected means no tools exposed to the model.

## Default Registry

`build_default_registry()` assembles tools in this order:

```text
fs tools
process/network tools
meta tools
wiki tools
generated prompt-directory agent tools
```

Generated agent tools come from directories with markdown files under repo
`prompts/`, global `agents/` in the platform config dir, and project `.js/agents/`. Project scope
wins over global, which wins over repo when roots define the same agent id. A
prompt directory whose name collides with an existing tool is skipped.

## Tool Context

`ToolContext` is mutable process-local state:

- `cwd`
- read limits and file size caps
- tool result and shell output caps
- fetch timeout
- vision enabled flag
- read-before-write state
- file hashes
- undo snapshots
- search cache
- todos

`run_turn()` hydrates the active context from `Config` each turn for output caps,
fetch timeout, agent id, selected registry, and vision mode.

Child task contexts copy limits and cwd from the parent but start with fresh
read sets, snapshots, todos, and search cache.

## Runtime Loop

`run_turn()` mutates the caller's `messages` list. It builds a provider `convo`
by prepending the system prompt to the current messages.
1. Builds `ai` message and tool parts via `js/model_client`.
2. Calls `model_client.stream_model(...)`.
3. Streams events; `model_client` aggregates text, reasoning content, and fragmented tool calls.
4. Parses each completed tool-call argument string with `_repair_jsonish`.
5. Dispatches through `call_tool` and appends tool-result messages.
6. Repeats until the model returns a stop or the tool-iteration cap is hit.
7. Appends tool result messages.
8. Stops on tool retry limit or max iterations.
Provider request retry:

- SDK `ProviderAPIError.is_retryable` permits two transport retries with backoff.
- Context overflow has three separate recovery rounds, each followed by a new
  request. Recovery clears old tool results, then summarizes if needed. This
  also works after tool execution: only the rejected model request is retried.
- Unrecovered provider errors propagate to the caller.

## Tool Dispatch

Tool call arguments arrive as JSON strings, often in fragments. The runtime
concatenates fragments by call id and repairs common JSON mistakes:

- outer JSON string that decodes to JSON
- trailing commas
- missing final `}` for object-shaped args

Unknown tool calls return an `ERROR` result naming available tools.

Tool errors are tracked per tool. A repeated `ERROR` gets retry metadata:

```text
<retry>attempts_left=2, allowed_max_attempts=3</retry>
```

After the retry limit is reached, the runtime appends a final assistant error
instead of surfacing "no assistant response".

## Task Parallelism

`task` calls from the same assistant turn are dispatched concurrently. Non-task
tools from that turn are dispatched sequentially. Result messages are restored
to original tool-call order before being appended.

Inside the `task` tool, multiple task strings also run concurrently using a
thread pool.

See [Subagents](subagents.md).

## Images And Vision

`read` returns special image markers when the model is vision-capable. The
runtime expands the marker into image bytes for the current model call, then
persists only a text stub in session history so base64 is not replayed forever.

Image result shape:

- the tool-result message carries only the text stub
- a following user message carries the same stub plus a `FilePart`
- persisted session history stores only the stub

## Built-In Modes

`--commit` is a convenience wrapper around the prompt-directory `commit` agent.

## Commit Helper

`js.commit_helper` is a standalone CLI the `commit` agent shells out to for the
deterministic parts of committing: surveying repo state and splitting one file
across commits. These belong in code, not the model
(`js/commit_helper.py:1`). Both subcommands accept `-C <dir>` / `--repo <dir>`
(or `--repo=<dir>`) before the subcommand to target a repo explicitly instead of
the process cwd (`js/commit_helper.py:292`). With no repo flag the helper resolves
`Path.cwd()` (`js/commit_helper.py:50`).

`python -m js.commit_helper survey` prints one compact snapshot the agent reads
once instead of probing (`js/commit_helper.py:170`):

- `branch:` line (`branch --show-current`, falling back to a detached-HEAD short
  hash or `(no commits yet)`) (`js/commit_helper.py:127`)
- `-- status --`: raw `git status --porcelain` `XY path` rows, or
  `(clean tree, nothing to commit)` (`js/commit_helper.py:181`)
- `-- staged diff --` and `-- unstaged diff --`: per-tracked-file text diffs with
  every `@@` hunk numbered, each headed `### <path>  (N hunks)`
  (`js/commit_helper.py:149`)
- `-- untracked --`: `??` files, each tagged with the `stage <p> all` hint
  (`js/commit_helper.py:198`)
- `-- recent log --`: `git log --oneline -8`, or `(no history)`
  (`js/commit_helper.py:205`)

The survey is deterministic: it only reads git state (status/diff/log), runs no
model, and does not mutate the repo (`js/commit_helper.py:170`). A file with no
text hunks prints `(no text hunks — binary/rename/mode? stage the whole file)`
(`js/commit_helper.py:160`).

`python -m js.commit_helper stage <file> <hunks|all>` stages part of one file
(`js/commit_helper.py:232`). `<hunks>` is a comma-separated list of the 1-based
hunk numbers the survey printed (e.g. `1,3`), or `all`:

- For a tracked text file, the named hunks are extracted from `git diff -- <file>`
  and replayed with `git apply --cached --recount`; output is
  `staged <file> hunk(s) 1,3 of N` (`js/commit_helper.py:276`).
- `all` on any file stages the whole file via `git add -- <file>`
  (`js/commit_helper.py:214`).
- An untracked (`??`) file only accepts `all`; a hunk spec on it errors
  (`js/commit_helper.py:245`).
- Out-of-range hunk numbers, non-numeric specs, files with no pending changes,
  and `git apply` failures each error with exit code `2` (or `1` for the apply
  failure, which also prints the `stage <file> all` fallback)
  (`js/commit_helper.py:271`).

For a bare or unknown subcommand `main()` prints the module docstring; exit is
`2` when no args were given and `0` when an unknown subcommand was passed
(`js/commit_helper.py:312`). `stage` with the wrong argument count exits `2`
(`js/commit_helper.py:317`). A non-git directory exits `2`; other git failures
exit `1` (`js/commit_helper.py:175`).

## Error Boundaries

The runtime generally returns tool failures to the model as strings rather than
raising. Provider and harness failures raise to the CLI.

The CLI prompt mode catches runtime exceptions and returns exit code `1`.

The REPL catches runtime exceptions and keeps the REPL alive. Completed tool
work and partial replies are persisted; an unstarted prompt is discarded using
its current position, including after compaction has shifted the history.

## Backward Compatibility Policy

This project intentionally does not preserve old tool aliases. The canonical
surface is the contract:

```text
browse browser_probe commit defaultagent docs_search exa_search fetch
fs_search patch plan read remove serper_search shell skill task
tavily_search terminal_session terminal_snapshot todo_read todo_write
undo wiki_convert wiki_finish_ingest wiki_write write
```

`multi_patch`, `sem_search`, `followup` and the `artifact` suite were removed
outright; `multi_patch`'s batch form now lives in `patch` as its `edits`
parameter.

Tests should protect current behavior, not old names.


## Context-window resolution

Remote model context limits come from models.dev. Catalog refresh keeps release
dates alongside limits in the local SQLite cache. Exact provider/model IDs are
resolved first; routed prefixes and effort suffixes are normalized. Family
`latest` aliases select the newest matching release date while preserving
variant names. Manufacturer rows resolve equal-release reseller duplicates.
A family lookup is an estimate of an alias target, not router configuration.

Ollama, llama.cpp and vLLM can use their actual allocated context. Explicit
model overrides remain available. `compact.context_window` applies a shared
window to the run banner and both automatic compaction paths;
`compact.context_window_fallback` defaults to 1,000,000 for unresolved models.
Existing jsrc values override that default.

Both automatic compaction paths cap reply headroom with
`compact.summary_reserve_tokens` and reserve `compact.buffer_tokens`.
Output truncation alone does not trigger summarization. Between-turn compaction
is awaited on the async CLI loop, so cancellation stops its provider request.
In-turn compaction
prints its reason and budget before summarizing; automatic compaction marks
retain phase, context-token count, window and effective input limit.


### Compaction flight records

Every call to the central compaction function writes a unique attempt under
`logs/<agent>/compactions/<session>-<attempt>.jsonl`. `compact.flight_log_dir`
changes that directory. Records contain caller stack/PIDs, active model and
settings (credential fields redacted), exact system/messages before and after,
content hashes, retention decision, summary input/output and provider trace,
plus success, skip, failure or cancellation. Runtime-triggered records also
include the tool schemas, request budget and usage anchor. Files are created
with mode 0600; records are flushed and fsynced before summarization starts.

Terminal START and outcome notices include attempt ID and file path. Session
compaction marks carry that same ID. Budget telemetry also goes to the existing
request autolog as `FLIGHT` JSON records even when optional runtime debug is off.
`/set compact.context_window N` immediately displays the effective next-request
window, and between-turn compaction reads the same live settings.

Overflow recovery also records `operation=tool-result-clearing` attempts before
replacing old tool-result bodies. START/outcome notices go to stderr even when
answer stdout is redirected. Flight data includes the provider rejection,
retry round, retained-result count, changed message indexes and tool-call IDs,
character savings, and complete before/after context. No eligible results is
recorded as SKIPPED; any following summarization has its own attempt ID.

### Compaction commit and replay

In-turn budget recovery clears old tool results first (`compact.clear_keep_recent`
starts the retained-result count), then summarizes older history. If the active
turn itself is too large, its older work can be summarized while retaining a
paired assistant/tool tail. Empty prefixes and summary-only prefixes are skipped.
Both trigger paths use current provider-anchored input plus generated output;
output-only usage falls back to estimation. Small windows share the same capped
reserve and buffer calculation.

Summary overflow partitions the source and summarizes both halves, with bounded
split depth. A failing partition, blank response, or incomplete response leaves
the source history intact. The proposed replacement must shrink the history;
optional file reattachment is omitted if it consumes those savings.

Pending messages are journaled before a compaction mark. The mark carries any
reattached files, and clearing mutations are journaled as replacements. CLI and
child-agent persistence compare the current live history with replayed history,
append changed suffixes, and retain original records in the archive. Cancellation
uses current user position rather than a pre-compaction list offset.

`tests/compaction_harness/` contains standalone adversarial runners: scripted
provider-boundary failures and a loopback HTTP/SSE server exercising the actual
SDK adapter. See [Compaction adversarial audit](compaction-adversarial-audit.md).
