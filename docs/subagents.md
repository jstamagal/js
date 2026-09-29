# Subagents

Subagents in `js` are implemented by the `task` tool and generated
prompt-directory tools. They are parallel worker turns. A `task` call waits for
them, or with `background=true` returns a handle at once.

Agents are created using prompt directories under `prompts/` (for bundled agents)
or `.js/agents/` (for project/global agents), with an `agent.yaml` manifest
declaring `tools:` (`noun:modifier` entries, see
[tool-system.md](tool-system.md)) and optional `model:`, `reasoning:` and
`max_tokens:` overrides.

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

- `agent_id` is required to start workers.
- `tasks` must be a list of strings.
- Empty task strings are ignored; an all-empty list is an error.
- `session_id` is optional and resumes that worker agent session.
- Recursive task depth defaults to `2` and is set by `limits.task_max_depth` in
  config or `--extra limits.task_max_depth=N`. (There is no `JS_TASK_MAX_DEPTH`
  env var.)

## Direct Agent Tools

Prompt directories under repo `prompts/`, global `~/.js/agents/`, and
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
<root>/<agent_id>/*.md  # root is repo prompts/, ~/.js/agents/, or project .js/agents/
```

Then selects tools from the full registry using that agent's `agent.yaml`
`tools:` entries. If the prompt directory is missing, the worker runs with an
empty system prompt and no tools.

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

## Wiki Agents And Built-In Artifact Mode

Wiki workers are ordinary prompt-directory agents selected with `--agent`; the
`wiki` wrapper routes friendly commands to installed `wiki-*` agents. Native
`wiki_convert`, `wiki_write`, and `wiki_finish_ingest` tools provide deterministic
ingestion operations. Artifact remains a built-in CLI mode.

## Predefined Subagent Types

There are no typed worker classes like `research`, `commit`, `wiki`, or
`reviewer` baked into the runtime.

What exists today:

- `task`: generic subagent runner.
- prompt-directory agents: any `<root>/<agent_id>` directory under repo
  `prompts/`, global `~/.js/agents/`, and project `.js/agents/`.
- generated direct tools for prompt directories.
- bundled prompt dirs: `defaultagent`, `autocoder`, `commit`.
- built-in CLI mode: commit, exposed as a prompt-directory agent and a
  `js --commit` wrapper.

## Foreground And Background

A foreground `task` call blocks until all child futures complete. One task
returns the worker's text verbatim; a fan-out returns one `TASK_RESULTS` string
with the results numbered in task order.

`background=true` starts the same fan-out and returns at once:

```text
task running in the background (handle t1): 2 tasks for agent `explore`. ...
When it finishes its result is written to <state>/tool-results/task-t1-<random>.txt, ...
HANDLE t1 RUNNING
```

The handle works like a shell handle, through the same tool:

- `action="poll", handle="t1"`: still running (with how many workers are done),
  or the result.
- `action="wait", handle="t1", timeout=N`: block up to N seconds; without
  `timeout`, until it ends.
- `action="kill", handle="t1"`: cancel the workers.
- `handle` defaults to the caller's newest running task.

Each run belongs to the context that started it (`ToolContext.task_owner`): the
main agent and each subagent see, poll, wait on and kill only their own runs.
`/reset` and `/wipe` drop the reminder for runs of the conversation they end.

Under the REPL the run is one `subagent` job on the REPL's loop
(`js/toolkit/task_jobs.py`), so it outlives the turn that started it, shows in
`/jobs`, and `/cancel <id>` stops it. Without a supervisor (`-p`) it runs on a
private loop in a daemon thread and ends with the process.

When a run started by the main agent finishes and the model has not read its
result through poll, wait or kill, the next user message carries a
`<js-reminder>` naming the handle and the result file (`cli._with_pending_notes`).
A run a subagent started gets no reminder; that subagent polls it itself.

Children run through the same `_prepare_fan_out` as a foreground call, so they
get the same inherited context and, under `-C`, the same jail.

Direct agent tools have no background form.

Not implemented:

- listing background tasks to the model
- per-task timeout setting
- model-facing per-task endpoint override

## Endpoint And Model Overrides

Subagents choose a model in this order:

1. the `task` tool's `model` argument, if the operator has not set
   `subagents.lock_model`;
2. the parent turn's current model, when `subagents.prefer_inherit` is true;
3. the child agent's `agent.yaml` `model:`;
4. the parent model as the fallback.

Put agent defaults in the prompt directory's `agent.yaml`:

```yaml
# Optional: pin this agent's default model and reasoning effort.
# A provider-prefixed id re-routes the child provider/base/key/headers through
# the model-route resolver; a bare id keeps the parent's provider route.
model: anthropic/claude-sonnet-4
reasoning: high  # off|minimal|low|medium|high|xhigh|max
tools:
  - read:eager
  - fs_search:eager
```

`reasoning:` sets the child default independently of the model. `off`
explicitly disables reasoning; omitting it inherits the parent/provider setting.
Invalid values fail agent loading rather than being silently ignored.

Top-level `js --agent <id>` also applies that agent's manifest `model:` through
the same route resolver. Operator pins win: `-m` / `--model`, `JS_MODEL`, or a
configured non-default `model.id` leave the agent manifest model unused.

`subagents.lock_model = true` (`Config.lock_subagent_model`) removes the `task`
tool's `model` parameter from both the model-facing description and the JSON
schema. The child can still use its own manifest `model:`; the parent model
just cannot override it through a tool call.

Do not expose raw endpoint URLs as normal model tool arguments unless the goal
is explicitly to let the model route traffic.
