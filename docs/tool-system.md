# Tool System

The tool system is the main contract between the model and the local machine.
It is intentionally close to Forge-style behavior: canonical tool names, rich
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
- todos

`call_tool()` filters unknown args, coerces values based on schema type, injects
`context` when the handler accepts it, and calls the handler.

## Registry Assembly

`build_default_registry()` concatenates:

1. filesystem tools
2. process/network tools
3. meta tools
4. wiki tools
5. artifact tools
6. generated agent tools from layered prompt roots

The registry stores canonical names and lowercased aliases. Current aliases are
only used for provider-facing name transforms; old user-facing aliases are not
kept.

## Prompt Selection

Prompt frontmatter selects tools:

```yaml
tools:
  - read
  - fs_search
  - todo_*
  - task
```

Selectors:

- exact name: `read`
- glob: `todo_*`
- full registry: `*`

No selected tools means no tools are exposed.

## Tool Descriptions

Model-facing descriptions live in:

```text
js/toolkit/tool_descriptions/<tool>.md
```

The filename must match the registered tool name for core/wiki/artifact tools.
Generated agent tools build descriptions at runtime.

These descriptions are not comments. They are model-facing contract text. The
Forge-style part is that the descriptions are explicit about ambiguity and
failure modes: when to read first, what line anchors mean, how to patch, when to
use `cwd`, how tasks run, and what not to infer.

Tests check description files for registered tools and protect the canonical
surface.

## Canonical Core Surface

```text
read
write
fs_search
sem_search
remove
patch
multi_patch
undo
shell
fetch
followup
plan
skill
todo_write
todo_read
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
forge__read_file
```

## Provider-Facing Names

When `"claude"` appears in the model string, only these schema names change:

```text
read  -> Read
write -> Write
task  -> Task
```

The active registry still resolves to canonical lowercase tools, and persisted
history stores lowercase names.

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
- `FOLLOWUP_REQUIRED` stops the loop and returns control to the caller

Inside the `task` tool, multiple task strings also run concurrently.

## File Safety

`read` records that a file was read and remembers its hash. `write`, `patch`,
and `multi_patch` require a prior read before changing existing files. New file
creation does not require a prior read.

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
- context line knobs: `-A`, `-B`, `-C`
- line numbers: `-n`
- case-insensitive: `-i`
- file type/extension
- head limit and offset
- multiline matching

`sem_search` is local term-ranked search. It does not call embeddings, a vector
database, or a network service.

## Shell

`shell` uses the environment shell:

- Unix: `$SHELL -c`, fallback `/bin/sh -c`
- Windows: `COMSPEC /C`

It passes a small allowlist of environment variables by default. Extra env var
names can be requested through the `env` parameter if the parent process has
them.

The tool output includes:

- shell path
- exit code
- optional description
- stdout
- stderr

ANSI is stripped unless `keep_ansi=true`.

## Wiki And Artifact Tools

Wiki and artifact tools live in the default registry even when the active prompt
does not select them. Built-in modes select the full registry and rely on their
mode prompts to steer behavior.

Defaultagent does not select `wiki_*` or `artifact_*` by default. To let a
normal prompt or subagent use those tools, add selectors to that prompt's
frontmatter.

## Generated Agent Tools

Every prompt directory with markdown files under repo `prompts/`, global
`agents/` in the platform config dir, and project `.js/agents/` becomes a direct agent tool unless
its name collides with a base tool. Project scope wins over global, which wins
over repo when the same agent id appears in multiple roots.

Direct generated tools take:

```json
{"tasks":["task text"]}
```

They call the same underlying `task` implementation with the agent id fixed.

## Porting Rules

For another Python project, the behavior to preserve is:

- canonical names with no legacy aliases
- descriptions as contract text
- prompt frontmatter tool selection
- `Tool` plus `ToolContext` separation
- read-before-write state in context
- exact patch/multi-patch behavior
- in-process undo snapshots
- `$SHELL`/`COMSPEC` shell execution
- task parallelism and child context isolation
- Claude provider-facing name transform based on model string only
- canonical persisted history
- capped tool results and shell output

See [Porting The Forge Tool System To Python](porting-forge-tool-system-to-python.md)
for an implementation checklist.
