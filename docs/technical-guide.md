# Technical Guide

This is the architecture guide for contributors and agents working on `js`.

## Package Entry Points

`pyproject.toml` exposes:

```toml
[project.scripts]
js = "js.cli:main"
js-drain = "js.drain:main"
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
| `js/cli.py` | argument parser, REPL, prompt/pipe mode, wiki/artifact/commit orchestration |
| `js/config.py` | environment parsing, session reservation, model/provider caps, vision heuristic |
| `js/model_client.py` | single import boundary for the Vercel AI Python SDK |
| `js/runtime.py` | streaming loop, tool-call aggregation, dispatch, provider quirks |
| `js/memory.py` | locked JSONL persistence and loader control marks |
| `js/persona.py` | prompt-directory concatenation and `tools:` frontmatter |
| `js/drain.py` | wiki inbox/folder drain planner, TUI, sequential job executor |
| `js/tools.py` | compatibility import of the default registry/context |
| `js/toolkit/core.py` | `Tool`, `ToolContext`, argument coercion, handler invocation |
| `js/toolkit/registry.py` | default registry assembly and selector matching |
| `js/toolkit/fs.py` | file read/write/search/edit/delete/undo tools |
| `js/toolkit/process_net.py` | shell and fetch tools |
| `js/toolkit/meta.py` | todo/followup/plan/skill/task and generated agent tools |
| `js/toolkit/wiki/` | wiki tools, helpers, mode prompts |
| `js/toolkit/artifact/` | artifact tools and mode prompts |

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
artifact tools
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
8. Stops on `FOLLOWUP_REQUIRED`, tool retry limit, or max iterations.
Provider transport retry:

- Retries `APIConnectionError`, `RateLimitError`, `ServiceUnavailableError`,
  `Timeout`, and 5xx `APIError`.
- Treats auth, not found, bad request, and context overflow as fatal.
- Uses exponential backoff with jitter.

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

`--wiki` and `--artifact` do not require prompt dirs. They build system prompts
from code constants and select the full registry. If `--agent` is provided,
that agent's persona is prepended to the built-in prompt.

`--commit` is different: it is a convenience wrapper around the prompt-directory
`commit` agent.

## Error Boundaries

The runtime generally returns tool failures to the model as strings rather than
raising. Provider and harness failures raise to the CLI.

The CLI prompt mode catches runtime exceptions and returns exit code `1`.

The REPL catches runtime exceptions, rolls back the appended user message with a
`rollback_to:N` mark, and keeps the REPL alive.

## Backward Compatibility Policy

This project intentionally does not preserve old tool aliases. The canonical
surface is the contract:

```text
read write fs_search sem_search remove patch multi_patch undo shell fetch
followup plan skill todo_write todo_read task
```

Tests should protect current behavior, not old names.
