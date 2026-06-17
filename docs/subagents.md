# Subagents

Subagents in `js` are implemented by the `task` tool and generated
prompt-directory tools. They are parallel worker turns, not long-running managed
processes with handles.

For how to *create* the agents this page runs — directory layout, id rules,
reserved names, and `tools:` frontmatter — see
[agents-and-prompts.md](agents-and-prompts.md).

## Generic `task`

Schema:

```json
{
  "tasks": ["inspect the runtime loop", "inspect the registry"],
  "agent_id": "autocoder",
  "session_id": "optional-session"
}
```

Rules:

- `agent_id` is required.
- `tasks` must be a list of strings.
- Empty task strings are ignored; an all-empty list is an error.
- `session_id` is optional and resumes that worker agent session.
- Recursive task depth defaults to `2` and is set by `limits.task_max_depth` in
  config or `--extra limits.task_max_depth=N`. (There is no `JS_TASK_MAX_DEPTH`
  env var.)

## Direct Agent Tools

Prompt directories under repo `prompts/`, global `agents/` in the platform config dir, and
project `.js/agents/` become direct tools. Project scope wins over global,
which wins over repo:

```json
{
  "tool": "autocoder",
  "arguments": {
    "tasks": ["implement this focused fix and report tests"]
  }
}
```

Direct tools call the same task implementation with the agent id fixed. They
only expose `tasks`; they do not expose `agent_id` or `session_id`.

The default prompt currently selects direct `autocoder` and `commit` tools.

## Parallelism

There are two layers of parallelism:

1. If the assistant emits multiple `task` tool calls in the same turn, the
   runtime dispatches those `task` calls concurrently.
2. Inside one `task` call, each task string runs concurrently in a thread pool.

Result order is stable. Output is returned in the order of the input task list.

If a child fails, the result slot for that child contains `ERROR ...`. Successful
sibling results are kept. A failing child does not sink its siblings.

Non-task tools in a turn run sequentially.

## Worker Session State

For each child task:

1. The parent config is copied with `dataclasses.replace`.
2. `agent_id`, `agent_dir`, `history_file`, `sessions_dir`, `session_file`, and
   `prompts_dir` are changed for the child agent.
3. If `session_id` is provided, that child session is opened or created under
   the child agent.
4. Otherwise a fresh `task-<timestamp>-<random>.jsonl` is reserved.
5. The child's new messages are appended to that child session after success.

## Tool Surface Isolation

Child agents do not inherit the parent's selected tool surface.

The child loads:

```text
<root>/<agent_id>/*.md  # root is repo prompts/, platform config agents/, or project .js/agents/
```

Then selects tools from the full registry using that prompt's `tools:`
frontmatter. If the prompt directory is missing, the worker runs with an empty
system prompt and no tools.

Child contexts copy:

- cwd
- read limits
- file size limits
- tool result cap
- shell output cap
- fetch timeout

Child contexts do not inherit:

- selected tool surface
- read-before-write set
- file hashes
- undo snapshots
- search cache
- todos

## Built-In Wiki/Artifact Modes Are Not Subagent Types

`js --wiki=...` and `js --artifact=...` are CLI modes with built-in prompt
builders. The generic `task` tool does not run a child "as if `--wiki` was
passed".

To make a wiki-capable subagent, create a prompt directory such as
`prompts/wiki-worker/00-tools.md`:

```markdown
---
tools:
  - read
  - fs_search
  - shell
  - wiki_*
---

You are a wiki worker. Start with wiki_purpose(vault), then perform the assigned
wiki task.
```

Then call:

```json
{
  "tasks": ["vault=creative; ingest the one specified inbox unit ..."],
  "agent_id": "wiki-worker"
}
```

That passes wiki tools by selection, not by inheriting the parent surface.

## Predefined Subagent Types

There are no typed worker classes like `research`, `commit`, `wiki`, or
`reviewer` baked into the runtime.

What exists today:

- `task`: generic subagent runner.
- prompt-directory agents: any `<root>/<agent_id>` directory under repo
  `prompts/`, global `agents/` in the platform config dir, and project `.js/agents/`.
- generated direct tools for prompt directories.
- bundled prompt dirs: `defaultagent`, `autocoder`, `commit`.
- built-in CLI modes: wiki/artifact/commit/drain, but only commit is exposed as
  a prompt-directory agent and `js --commit` wrapper.

## No Monitor/Stop Handles Yet

A `task` call blocks until all child futures complete. The parent model receives
one aggregated `TASK_RESULTS` string.

Not implemented:

- progress handles
- polling child status
- stopping a running child
- listing active children
- per-task timeout knob
- max-worker knob
- model-facing per-task model override
- model-facing per-task endpoint override

Those can be added later as runtime-managed job handles, but the current system
does not have them.

## Endpoint And Model Overrides

Subagents currently inherit the parent config's provider route and model. The
child prompt cannot request a different model or endpoint through the `task`
schema.

A future implementation should keep endpoint/model routing server-side, for
example:

```toml
[agents.commit]
model = "openai/auto/gpt-5.5"
api_base = "http://127.0.0.1:8317/v1"

[agents.research]
model = "anthropic/claude-sonnet-4"
```

Then the model asks for `agent_id="commit"` and the harness chooses the route.
Do not expose raw endpoint URLs as normal model tool arguments unless the goal
is explicitly to let the model route traffic.
