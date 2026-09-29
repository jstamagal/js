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
   `prompts/`, global `~/.js/agents/`, and project `.js/agents/`.
4. `ToolRegistry.select()` filters the default registry by prompt selectors.
5. Existing session messages are loaded through `js.memory.load_replay_messages()`,
   which keeps every assistant's reasoning for replay.
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
| `js/usage.py` | per-session token and cost totals, the `usage` records, the meter a turn charges its calls to |
| `js/headless.py` | `js -p --json`: runtime events as JSON lines (docs/headless-json.md) |
| `js/messages.py` | every string the operator reads, as named entries; the banner slot; severity colours |
| `js/persona.py` | prompt-directory concatenation and `agent.yaml` |
| `js/events.py` | event names, `on` handler table, handler refusals |
| `js/hookexec.py` | `exec`: runs a command for the command layer; event JSON on stdin, exit 2 refuses |
| `js/prompt_commands.py` | `~/.js/commands/NAME.md` as `/NAME`, argument placeholders |
| `js/pastes.py` | large bracketed pastes kept behind `[paste #N ...]` markers |
| `js/toolkit/core.py` | `Tool`, `ToolContext`, argument coercion, handler invocation |
| `js/toolkit/registry.py` | default registry assembly, per-agent surfaces, lazy catalog |
| `js/toolkit/policy.py` | `noun:modifier` chains, `tools.yaml` tags and argument bans, `/tools` table |
| `js/toolkit/fs.py` | file read/write/search/edit/delete/undo tools |
| `js/toolkit/process_net.py` | shell and fetch tools |
| `js/toolkit/meta.py` | plan/skill/task and generated agent tools |
| `js/toolkit/wiki/` | deterministic tools for installed wiki agents |

## Prompt Loading

Prompt files (`*.md`, minus `NN-benchmark.md`) are sorted by filename and
concatenated with blank lines. The manifest is `agent.yaml` in the same
directory:

```yaml
model: cpa/claude-fable-5-1
reasoning: high
tools:
  - read:eager
  - fs_search:eager
  - "wiki_*:lazy"
  - task:eager
```

`tools` entries are `noun:modifier` (`eager`, `lazy`, `ban`) or `tag:NAME`;
resolution and `tools.yaml` are described in [tool-system.md](tool-system.md).
No entries means no tools exposed to the model. A `00-tools.yaml` or a
frontmatter `00*.md` fails the load with a line naming `agent.yaml`; `just
migrate-agents` converts them.

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
`prompts/`, global `~/.js/agents/`, and project `.js/agents/`. Project scope
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

`run_turn()` hydrates the active context from `Config` each turn for output caps,
fetch timeout, agent id, selected registry, and vision mode.

Child task contexts copy limits and cwd from the parent but start with fresh
read sets, snapshots, and search cache.

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
The model-client stream scope owns HTTP response byte iterators for the OpenAI
and Anthropic SDKs. `stream_transport.py` closes each iterator before its pool
entry on normal completion or cancellation. This covers SSE termination before
HTTP EOF with httpx2 2.12 (upstream pydantic/httpx2#1195). Custom transports are
left as supplied. Recheck this adapter when upstream stream ownership changes.

The same wrapper carries the `ui.net` channel. `stream_model_async` opens a
`NetCall` per request (`stream_transport.begin_call`); the response iterator
counts bytes into the caller's `TurnStatus` until the first token, and a
`trace` request extension reports the TCP/TLS handshake as "connected" (the
first token stands in when the transport cannot say). Who is calling (main
turn, `Subagent N`, `Compacting`) is a context variable set by
`run_turn_async` and the compaction call sites. `run_turn_async` retries, so
its role holds each request failure instead of printing it: the next request
drops it, and the turn prints it (level 1) only when it gives up. After each
response `compaction.note_response` compares its cache-read tokens with the
previous response of the same conversation and model; a drop of more than 5%
and at least 2000 tokens prints one cache-break line at level 2. A compaction or
tool-result clearing resets that baseline, because the drop it causes is
expected; so do `/reset` and `/wipe`. A response without usage keeps the
baseline. The channel
prints only while the async REPL has installed a sink; elsewhere every hook is
a no-op, and the models.dev refresh lines print to stderr as before.

`run_turn_async` also starts a usage meter (`js/usage.py`, a context variable)
naming the session file and the `ToolContext.usage_chain` above it. Each
model call it makes, and each compaction summary made inside it, is charged
to those sessions; a summary made outside a turn is charged to the session it
compacts. The meter's callback adds the call to the turn's totals, which
`turn_end` carries, and hands it to the `event_sink` as a `usage` event.

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

## Task and Read-Only Parallelism

`task` calls from the same assistant turn are dispatched concurrently. The
other calls from that turn run in model order under a readers-writer rule:
read-only calls next to each other run together, up to
`runtime.max_parallel_tools` at once (default 8; 1 runs every call in turn),
and a call that writes runs alone, after every call before it. Read, read,
patch, read runs as [read ∥ read] → patch → read. Result messages are restored
to original tool-call order before being appended. See
[Dispatch Semantics](tool-system.md#dispatch-semantics) for which calls count
as read-only and how shared state is kept consistent.

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
once instead of probing (`cmd_survey`). Its lines are `SURVEY_*` entries in
`js/messages.py`:

- `Branch:` line (`branch --show-current`, falling back to `Detached HEAD at
  <hash>` or `No commits yet`)
- `-- Status --`: raw `git status --porcelain` `XY path` rows, or
  `Clean tree. Nothing to commit.`
- the staged and the unstaged diff sections: per-tracked-file text diffs with
  every `@@` hunk numbered, each headed `### <path>: N hunks`. Only the
  unstaged heading names the `stage <file> <n[,n]|all>` form, since only
  unstaged hunk numbers address `stage`.
- `-- Untracked --`: `??` files, each with the `stage <p> all` hint, or `None.`
- `-- Recent log --`: `git log --oneline -8`, or `No history.`

The survey is deterministic: it only reads git state (status/diff/log), runs no
model, and does not mutate the repo. A file with no text hunks says so and
tells the agent to stage the whole file.

`python -m js.commit_helper stage <file> <hunks|all>` stages part of one file
(`cmd_stage`). `<hunks>` is a comma-separated list of the 1-based hunk numbers
the survey printed (e.g. `1,3`), or `all`:

- For a tracked text file, the named hunks are extracted from `git diff -- <file>`
  and replayed with `git apply --cached --recount`; output is
  `Staged <file>: hunks 1,3 of N`.
- `all` on any file stages the whole file via `git add -- <file>`.
- An untracked (`??`) file only accepts `all`; a hunk spec on it fails.
- Out-of-range hunk numbers, non-numeric specs and files with no pending
  changes each fail with exit code `2`; a `git apply` failure exits `1` and
  prints the `stage <file> all` fallback.

For a bare or unknown subcommand `main()` prints `COMMIT_HELPER_HELP`; exit is
`2` when no args were given and `0` when an unknown subcommand was passed.
`stage` or `commit` with the wrong argument count exits `2`. A non-git
directory exits `2`; other git failures exit `1`.

## Error Boundaries

The runtime generally returns tool failures to the model as strings rather than
raising. Provider and harness failures raise to the CLI.

The CLI prompt mode catches runtime exceptions and returns exit code `1`.

The REPL catches runtime exceptions and keeps the REPL alive. Completed tool
work and partial replies are persisted; an unstarted prompt is discarded using
its current position, including after compaction has shifted the history.

## Operator Messages

The operator's messages are named `Message` entries in `js/messages.py`; code
passes the values for their holes (`msgs.say(msgs.MODEL_SET, model=m)`,
`msgs.warn(...)` for stderr, `.text()` for a string raised as exception text).
A banner entry prints behind the `BANNER` slot (`***` today); no template
spells the slot. Severity is colour, never a word: `WARN` paints the holes
light yellow, `GRAVE` light red, and a message with no holes is painted whole.
`say` paints only a stream that is a terminal, so a pipe gets plain lines.

`Message.said(...)` is `.text()` that keeps its entry: a `str` whose `message`
and `fields` say what produced it. A REPL command returns its refusal that
way, and the REPL prints it in that entry's severity, so a usage slip is not
painted while a failed save is. `compact_now` returns its result the same way,
and `compaction.compacted()` tells a compaction from a skip by the entry.

The `/help` column (`CMD_*`), `js --help` (`SHORT_HELP`), every argparse help
and description (`OPT_*`) and argparse's own headings and refusal go through
entries too; `msgs.ArgumentParser` wires the last two. `SettingSpec` docs stay
in the settings registry beside their key, and the kernel panel's field
labels stay in the panel. The commit helper prints its entries with
`.text()`, because the commit agent reads its output: no colour, no slot.
Tool results, tool descriptions and prompts are the model's text and stay
where they are.

`tests/test_messages.py` fails on a `print`, `.print` or stream write in
`js/` that carries the slot, `error:`, `warning:`, `js:`, `knob` or `(no `;
on a literal argparse help, description, `parser.error` or command doc; and
on a setting doc with a paren aside. It drives `/help`, `--help`,
`--help-full`, a bad flag, the `/tools` table, a failed prompt directive and
the commit helper to check their output.

## Backward Compatibility Policy

This project intentionally does not preserve old tool aliases. The canonical
surface is the contract:

```text
browse browser_probe commit defaultagent docs_search exa_search fetch
fs_search patch plan read remove serper_search shell skill task
tavily_search terminal_session terminal_snapshot undo wiki_convert wiki_finish_ingest wiki_write write
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
`compact.context_window_fallback` is the window assumed for unresolved models;
`js/jsrc` sets it to 1,000,000.

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

In-turn budget recovery clears old tool results (`compact.clear_keep_recent`
starts the retained-result count), summarizes the history before the current
user message, and, if the active turn itself is too large, summarizes its older
work while retaining a paired assistant/tool tail. Empty prefixes and
summary-only prefixes are not summarized. The order depends on the prompt cache.
When the last request is at least `compact.cache_ttl_seconds` old (default 300,
0 = always), or the provider has already refused the request, clearing runs
first. While the cache is warm, the summary of earlier turns runs first, and
clearing runs only if that summary did not bring the request under budget.
Clearing always runs before the current turn is summarized, because that summary
rewrites the whole history too.

`compact.max_summary_failures` (default 3) automatic summaries that fail in a
row pause automatic compaction, in-turn and between turns, and print one line
saying so. One budget check makes at most one failed summary attempt. While
paused, tool-result clearing still runs, warm cache or not. A successful
summary, such as a manual `/compact`, resets the count and resumes it.
Both trigger paths use current provider-anchored input plus generated output;
output-only usage falls back to estimation. Small windows share the same capped
reserve and buffer calculation.

The summary request is plain text, not JSON: one `[User]`, `[Assistant]`,
`[Assistant tool calls]` or `[Tool result name]` paragraph per message inside
`<conversation>`. Tool results, tool-call arguments and reasoning longer than
`compact.summary_tool_result_chars` (default 2000, 0 = whole) keep their head
and tail. When the prefix starts with an earlier `<compaction-summary>`, that
summary goes in `<previous-summary>` and the model is asked to update it with
the new messages, so a second compaction carries the first one forward instead
of summarizing it as conversation. A re-attached files message is reduced to its
paths.

Summary overflow partitions the source and summarizes both halves, with bounded
split depth. The previous summary travels with the older half only. A failing partition, blank response, or incomplete response leaves
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
