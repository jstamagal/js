# Tool System

The tool system is the main contract between the model and the local machine.
Canonical tool names, rich
unambiguous model-facing descriptions, exact edit tools, read-before-write
guards, task parallelism, and provider-specific name handling at the boundary.

## Core Types

`Tool` in `js/toolkit/core.py` contains:

- `name`
- `description`
- `handler`
- JSON-schema-like `params`
- `required`
- `aliases`

`Tool.openai_spec()` returns an OpenAI function-tool schema with
`additionalProperties: false`.

`ToolContext` carries mutable runtime state:

- current working directory
- read/file/tool result limits
- shell output cap
- fetch timeout
- vision flag
- read-before-write paths
- file hashes
- undo snapshots
- search result cache

`call_tool()` filters unknown args, coerces values based on schema type, injects
`context` when the handler accepts it, and calls the handler.

## Registry Assembly

`build_default_registry()` concatenates:

1. filesystem tools
2. process/network tools
3. meta tools
4. wiki tools
5. generated agent tools from layered prompt roots

The registry stores canonical names and lowercased aliases. Current aliases are
only used for provider-facing name transforms; old user-facing aliases are not
kept.

## Tool Selection: `agent.yaml` and `tools.yaml`

Each agent directory has an `agent.yaml` beside its prompt files:

```yaml
model: cpa/claude-fable-5-1
reasoning: high
tools:
  - tag:code_hacker
  - fetch:lazy
skills:
  - engineering:*
```

Every `tools:` entry is `noun:modifier`. The noun is a tool name or a glob
over tool names (quote a glob that starts with `*`: `"*:ban"`). The modifier
is one of:

- `eager`: in the boot surface, published from the first model call
- `lazy`: listed in the `tool_discovery` catalog and loadable from it
- `ban`: not on this agent's surface

`tag:NAME` expands the tag's entries in place. Tags are defined in
`~/.js/tools.yaml`:

```yaml
tags:
  code_editor:
    - "*:ban"
    - read:eager
    - write:eager
    - docs_search:lazy
  code_hacker:
    - shell:eager
    - tag:code_editor
ban:                  # argument patterns, refused before dispatch
  shell: ["rm -rf", "git reset --hard"]
  fetch: ["*://www.example.com/*"]
```

`tag:read_only` is intrinsic: it matches every tool whose `read_only` property
is true (tools that write nothing). It takes an optional modifier,
`tag:read_only:lazy`; without one it means eager.

Resolution: the first entry that names a tool (its exact name or an intrinsic
tag it carries) decides it. Only when nothing names the tool does the first
matching glob decide. So `"*:ban"` is deny-by-default and a name allows past
it, while two names resolve by position: in `[tavily_search:ban,
tag:read_only]` the ban fires. A tool no entry matches is not on the surface.
js has no built-in policy; the chain is what the files say. `/tools` in the
REPL prints every tool with its state and the entry that decided it.

`tool_discovery` is published only when the surface has lazy tools, skills,
or MCP servers. Its native catalog is exactly the agent's lazy set.

`skills:` (in `agent.yaml` and `tools.yaml`) is validated as `family:name` or
`family:*` and stored on the prompt spec. It does not filter the skill
catalog yet: js skills have no family grouping, so every discovered skill is
listed regardless of `skills:`.

An entry that is not `noun:modifier`, an unknown tag, or a tag cycle fails
the agent load with one line. An exact noun that names no tool prints one
`matched no tool; ignoring` line naming the entry.

The `ban:` block is checked before dispatch: a call whose string argument
contains a plain pattern (or matches a glob pattern whole), ignoring case,
returns one `ERROR:` line naming the pattern and runs nothing.

`00-tools.yaml` and `tools:` frontmatter in a `00*.md` file are no longer
read; an agent dir that has one fails to load with a line naming
`agent.yaml`. `just migrate-agents` (dry run; `--apply` to write) converts
them: bare selectors become `NAME:eager`, `reasoning_effort` becomes
`reasoning`, and an entry that matches no tool is dropped and named. An
entry is kept when it is a `tag:` entry, a glob that matches at least one
tool, or a name that is a tool or an agent in the repo prompts, the global
agents dir or a root given on the command line. The same pass drops dead
entries from agents that already have `agent.yaml`. The ~/.js home migration
runs this conversion on every agent it moves (`js/agent_migration.py`).

## Tool Descriptions

Model-facing descriptions live in:

```text
js/toolkit/tool_descriptions/<tool>.md
```

Descriptions are cut to what the model can act on; `just tool-bytes` shows
what each costs.

The filename must match the registered tool name for core/wiki tools.
Generated agent tools build descriptions at runtime.

These descriptions are not comments. They are model-facing contract text,
explicit about ambiguity and failure modes: when to read first, what line anchors mean, how to patch, when to
use `cwd`, how tasks run, and what not to infer.

Tests check description files for registered tools and protect the canonical
surface.

## Canonical Core Surface

```text
read
write
fs_search
ast_search
remove
patch
undo
shell
fetch
plan
skill
task
```

The registry intentionally exposes only canonical names. Do not add compatibility
aliases for these non-canonical spellings:

```text
fs_read
fs_write
fs_list
semantic_search
cat
grep
```

## Provider-Facing Names

Tool names remain canonical by default. There is no automatic Claude-specific capitalization.

Optional `tools.alias_profiles` settings map canonical names to model-facing aliases. Each profile contains a `match` string or list of strings and an `aliases` table. Matching is case-insensitive substring matching against the model ID and provider ID. The first matching profile with usable aliases wins; without a matching profile, names remain unchanged.

The runtime rewrites outgoing tool schema names and backtick-wrapped tool-name references in descriptions. The active registry resolves aliases back to canonical handlers, and tool-call history records canonical names.

## Dispatch Semantics

The model response stream can contain text and fragmented tool calls. Runtime
aggregation preserves first-seen call order and concatenates argument chunks by
tool call id.

Dispatch rules:

- task calls from one assistant turn run concurrently
- non-task calls run sequentially
- results are appended in original tool-call order
- tool result content is capped
- repeated tool errors get retry metadata
- a repeated-error limit appends a final assistant error

Inside the `task` tool, multiple task strings also run concurrently.

## Lazy MCP Host

Configured MCP servers join the turn surface through the eager canonical
`tool_discovery` tool, not by adding every remote schema at startup. Before an
MCP-scoped discovery, no server process or HTTP connection is opened and adding
more configured servers does not enlarge the emitted tool schemas beyond the
fixed discovery schema. Discovery initializes only eligible candidates (an exact
`source` can select one); loading `mcp:<server>__<tool>` adds that remote schema
for the rest of the current turn.

Remote names are normalized and namespaced as
`<normalized_server>__<normalized_tool>`. Tool allow/deny policy applies to that
public name. `notifications/tools/list_changed` marks the catalog dirty; it is
refetched before the next model call, so an already loaded tool receives the new
schema without becoming callable before the model sees it. Tool calls are
non-replayable: transport death can reconnect a later safe list/read/get request,
but never silently duplicates the failed call.

Resources and prompts use the canonical controls listed in
[tools-reference.md](tools-reference.md). MCP clients and loaded names live for a
turn unless a caller supplies a session-owned host; clients in that host may be
reused across turns, while each new turn still starts with no loaded MCP schemas.
Owned hosts close stdio children and streamable-HTTP sessions at turn end.

Skills can request the discovery surface in frontmatter:

```yaml
---
description: Inspect remote project data
tools:
  - tool_discovery
---
```

A skill names only canonical local tools; remote MCP tools remain dynamically
discovered and loaded by catalog id.

## File Safety

`read` records that a file was read and remembers its hash. `write` and `patch`
require a prior read before changing existing files. New file creation does not
require a prior read.

Edit operations snapshot prior state for `undo`:

- existing file bytes
- nonexistence before creating a file
- directory trees before removing a directory

`undo` is in-process only. Snapshots are not persisted across process restarts.

## Search

`fs_search` is regex search implemented in Python. It supports:

- `pattern`
- `path`
- `glob`
- output modes: `files_with_matches`, `content`, `count`
- context line options: `-A`, `-B`, `-C`
- line numbers: `-n`
- case-insensitive: `-i`
- file type/extension
- head limit and offset
- multiline matching

`ast_search` is structural source search backed by ast-grep. It accepts parsed
patterns with `$NAME` and `$$$ARGS` metavariables, emits read-compatible line
anchors, and supports snapshot-backed structural rewrite previews and applies.

## Shell

`shell` runs the `shell.program` setting:

- Unix: `bash -o pipefail -c` by default; zsh also gets `-o pipefail`, other
  shells run with `-c` alone
- Windows: `COMSPEC /C`

It passes a small allowlist of environment variables by default. Extra env var
names can be requested through the `env` parameter if the parent process has
them. A result names the filtered variables only when the command references
one of them.

The tool output includes:

- shell path
- exit code
- optional description
- stdout
- stderr

ANSI is stripped unless `keep_ansi=true`.

## Wiki Tools

Wiki tools live in the default registry even when the active prompt does not
select them. Built-in modes select the full registry and rely on their mode
prompts to steer behavior.

Defaultagent does not select `wiki_*` by default. To let a normal prompt or
subagent use those tools, add entries to that agent's `agent.yaml`.

## Generated Agent Tools

Every prompt directory with markdown files or an `agent.yaml` under repo `prompts/`, global
`~/.js/agents/`, and project `.js/agents/` becomes a direct agent tool unless
its name collides with a base tool. Project scope wins over global, which wins
over repo when the same agent id appears in multiple roots.

Direct generated tools take:

```json
{"tasks":["task text"]}
```

They call the same underlying `task` implementation with the agent id fixed.

## Porting Rules

For another Python project, the behavior to preserve is:

- canonical names only in the public registry
- descriptions as contract text
- `agent.yaml` tool selection
- `Tool` plus `ToolContext` separation
- read-before-write state in context
- exact patch/multi-patch behavior
- in-process undo snapshots
- `shell.program`/`COMSPEC` shell execution
- task parallelism and child context isolation
- Claude provider-facing name transform based on model string only
- canonical persisted history
- capped tool results and shell output
