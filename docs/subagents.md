# Subagents

Subagents in `js` are implemented by the `task` tool and generated
prompt-directory tools. They are parallel worker turns. The main agent's call
starts them and returns their handles at once; it lists, polls, waits on,
kills and sends messages to them through the `task` tool, and a finished
result comes back to it on its own.

Agents are created using prompt directories under `defaults/agents/` (for bundled agents)
or `.js/agents/` (for project/global agents), with an `agent.yaml` manifest
declaring `tools:` (`noun:modifier` entries, see
[tool-system.md](tool-system.md)) and optional `model:`, `reasoning:` and
`max_tokens:` overrides.

## Generic `task`

Schema:

```json
{
  "tasks": ["inspect the runtime loop", "inspect the registry"],
  "agent_id": "slim",
  "session_id": "optional-session"
}
```

Rules:

- `agent_id` is required to start workers.
- Each directory name in an agent ID uses letters, digits, underscores and
  hyphens. Join nested names with a colon, e.g. `"agent_id": "meta:workflow-creator"`.
- `tasks` must be a list of strings.
- Empty task strings are ignored; an all-empty list is an error.
- `session_id` is optional, takes a single task, and resumes that worker agent
  session.
- A session with an active worker refuses another concurrent run.
- Recursive task depth defaults to `2` and is set by `SUBAGENT_DEPTH_MAX` in
  config or `--extra SUBAGENT_DEPTH_MAX=N`. (There is no `JS_TASK_MAX_DEPTH`
  env var.)

## Direct Agent Tools

The `slim` agent can be called directly as a tool.

```json
{
  "tool": "slim",
  "arguments": {
    "tasks": ["implement this focused fix and report tests"]
  }
}
```

Direct tools call the same task implementation with the agent id fixed. They
expose `tasks`, `model` and `reasoning`; they do not expose `agent_id` or
`session_id`.

For nested agents, use a double underscore between directory names in the
tool name. An `agent.yaml` entry of `meta__workflow-creator:eager` exposes
the `meta:workflow-creator` agent as the tool `meta__workflow-creator`.
For a name collision or a generated tool name over 64 characters, use
`task` with the colon-separated ID. Top-level agents keep their tool names.

The stock `defaultagent` selects the `task` tool and the direct `commit` tool.

## Parallelism

There are two layers of parallelism:

1. If the assistant emits multiple `task` tool calls in the same turn, the
   runtime dispatches those `task` calls concurrently.
2. Inside one `task` call, each task string runs as a coroutine on the parent's
   event loop.

`SUBAGENT_WORKERS_MAX` caps how many subagents run at once in the whole
process: every depth, every `task` call, foreground and background together.
Past the cap a subagent waits for a slot. A subagent blocked on its own
foreground `task` call (or on `action="wait"`) lends its slot to its children
and takes one back when they are done, so with `SUBAGENT_DEPTH_MAX 4` and
`SUBAGENT_WORKERS_MAX 4` the tree can grow to 4 + 16 + 64 + 256 subagents but
no more than 4 of them work at the same time.

Result order is stable. Output is returned in the order of the input task list.

If a child fails, the result slot for that child contains `ERROR ...`. Successful
sibling results are kept. A failing child does not sink its siblings.

Foreground child turns share the parent's event loop in both the REPL and
one-shot mode, so cancelling the parent cancels its foreground children.
Synchronous leaf tools run in dispatch threads; read-only groups can run in
parallel.

Each child says when it starts and when it ends, one line each, on stderr in
`-p` and in the REPL's scrollback, whatever `TOOLS_DISPLAY` is:

```
*** Subagent 1 of 2 started: explore (named agent)  cpa/claude-opus-5-5  effort medium  12 tools  session task-20261105-abcd
*** Subagent 1 of 2 ok: explore  1m 13s  12,345 tok in  678 tok out
```

In the async REPL the launch line also names the subagent's window: each
subagent gets one (2 to 9) that shows everything it streams and takes lines
typed in it (user guide, "Windows").

The kind is `task`, `named agent` or `background task`. Model, effort, tool
count and session are the child's own, after its `agent.yaml` and the tool
call's `model`/`reasoning` are applied; the effort is what its requests send.
The end line says `ok`, `error` or `cancelled`. The lines are the output
events `subagent_start` and `subagent_end` (`js/output.py`), formatted through
the `SUBAGENT_STARTED` and `SUBAGENT_FINISHED` entries. Swarm agents run on
their own path (`js/swarm_run.py`) and report through the swarm's event
stream instead.

## Worker Session State

For each child task:

1. The parent config is copied with `dataclasses.replace`.
2. `agent_id`, `agent_dir`, `sessions_dir`, `session_file`, and `prompts_dir`
   are changed for the child agent.
3. If `session_id` is provided, that child session is opened or created inside
   the parent session's subagent directory.
4. Otherwise a fresh `task-<timestamp>-<random>.jsonl` is reserved.
5. The child's new messages are persisted on success, error, or cancellation.
   Its terminal sessions and private Python kernel are closed when it exits.

## Tool Surface Isolation

Child agents do not inherit the parent's selected tool surface.

The child loads `*-prompt.md` and `*-env.md` from its own agent directory
in the nearest project, home or stock layer that defines it. Its `agent.yaml`
`tools:` entries select tools from the full registry. A missing prompt
directory returns an error before creating a worker session.

Child contexts copy:

- cwd
- model
- every `on_context` row of `js/turn_settings.py` (read and file limits,
  tool result and shell output caps, network timeouts, shell, kernel, jail,
  lsp and notebook settings)

Child contexts do not inherit:

- selected tool surface
- read-before-write set
- file hashes
- undo snapshots
- search cache

## Wiki Agents

Wiki workers are ordinary prompt-directory agents selected with `--agent`; the
`wiki` wrapper routes friendly commands to installed `wiki-*` agents. Native
`wiki_convert`, `wiki_write`, and `wiki_finish_ingest` tools provide deterministic
ingestion operations.

## Predefined Subagent Types

There are no typed worker classes like `research`, `commit`, `wiki`, or
`reviewer` baked into the runtime.

What exists today:

- `task`: generic subagent runner.
- prompt-directory agents under stock `defaults/agents/`, global
  `~/.js/agents/` and project `.js/agents/`.
- generated direct tools for prompt directories.
- bundled prompt dirs: `commit`, `defaultagent`, `slim`.
- built-in CLI mode: commit, exposed as a prompt-directory agent and a
  `js --commit` wrapper.

## Launch, Watch, Stop, Talk

The shape follows what other harnesses give their main agent. Claude Code's
`Agent` tool runs a subagent in the background (`run_in_background`), the
parent is notified when it completes, `SendMessage` continues an agent by id
(a running one reads it, a finished one resumes with its context) and
`TaskStop` stops one. Codex CLI's multi-agent tools are `spawn_agent`,
`send_input` (steer a running agent), `wait_agent`, `list_agents` and
`close_agent` ([Codex subagents](https://developers.openai.com/codex/subagents.md),
[multi-agent v2 write-up](https://codex.danielvaughan.com/2026/03/30/codex-cli-multi-agent-v2-path-addressing-structured-messaging)).
opencode 2.x replaced the background-agents plugin, whose `delegate`,
`delegation_list` and `delegation_read` returned an id at once and sent a
notification on completion, with native background subagents
([plugin README](https://github.com/kdcokenny/opencode-background-agents)).
js keeps one `task` tool with actions, as its shell and kernel handles do.

**Launch.** A `task` call (or a named-agent call) of the main agent starts its
workers as a task job and returns at once. `background` unset means on where
something hands the context a result it left running
(`ToolContext.delivers_task_results`): the REPL's main agent, and a `-p` run
with `SUBAGENT_WAKE` on. Elsewhere (a swarm agent, a program on
`Conversation`, a subagent) it waits:

```text
task t1 started: 2 subagents of agent `explore` (t1.1, t1.2), running while you work.
...
HANDLE t1 RUNNING
```

`t1` is the job, `t1.K` its K-th worker. `background=false` waits and returns
the result: one task returns the worker's text verbatim, a fan-out one
`TASK_RESULTS` string numbered in task order. A subagent's own calls wait
unless they pass `background=true`, since a subagent's turn ends with its
answer and nobody would hand it a later result.

**Watch.** `action="list"` lists the caller's jobs and each worker's state.
`action="poll", handle="t1"` returns, while it runs, each worker's
`task_jobs.Worker.report`: state (waiting, running, done, error, stopped),
seconds, its tool calls so far and the latest few, the tail of the text it last
wrote and its session file; once the job is done, the result.
`handle="t1.2"` polls one worker. `action="wait", handle="t1", timeout=N`
blocks up to N seconds, or until it ends. In the REPL every worker also has a
window (`subagent_windows`), and `/jobs` lists the handles with progress.

**Stop.** `action="kill", handle="t1"` cancels the job. `handle="t1.2"`
cancels that worker alone (`Worker.stop`): its slot in the result reads
`STOPPED`, and the others carry on. ^C on the main turn leaves background jobs
running; `/cancel <id>` stops one by its supervisor job id.

**Talk.** `action="send", handle="t1.2", message="..."`:

- a running worker gets it in its inbox (`Worker.inbox`), joined with any line
  typed in its window, as a user message at its next tool boundary (the
  child turn's `steer`). What arrives after its last tool call it answers in
  a further turn before the job has its result, and its result is the
  answer it had given followed by that turn's;
- a finished worker answers it in a new turn on its own session file, with
  everything it did before: the call starts a new job with `session_id` set to
  that file and returns its handle. A worker whose parent session is not saved
  has no session to continue.

A job of several workers needs the worker's handle to send to; a job of one
takes `t1` too.

Each job belongs to the context that started it (`ToolContext.task_owner`): the
main agent and each subagent see and act on only their own. `/reset` and
`/wipe` drop the reminder for jobs of the conversation they end.

Under the REPL a job is one `subagent` job on the REPL's loop
(`js/toolkit/task_jobs.py`), so it outlives the turn that started it. Without
a supervisor (`-p`) it runs on a private loop in a daemon thread.

**Results.** When a job started by the main agent finishes and the model has
not read its result through poll, wait or kill, a `<js-reminder>` hands the
model the result (cut at `TOOL_RESULT_MAX`; the file holds all of it),
whichever comes first:

- the main agent's next tool boundary, if a turn is running (the REPL's and
  `-p`'s `steer`);
- the next user message (`repl._take_pending_notes`);
- with `SUBAGENT_WAKE` on (default), a turn of its own: in the REPL, when the
  main agent is idle and nothing is queued (`task_jobs.listener`, the
  consumer's `_WAKE`); in `-p`, the run waits for every outstanding job after
  the prompt's turn and runs one turn per batch of results
  (`prompt_run._background_turns`), so the run's answer is the last of them.
  With it off, `-p` ends with jobs still running and they end with the process.

A job a subagent started gets no reminder; that subagent polls or waits on it.

Not implemented:

- per-task timeout setting
- model-facing per-task endpoint override

## Endpoint And Model Overrides

Subagents choose a model in this order:

1. the `task` tool's `model` argument, if the operator has not set
   `SUBAGENT_MODEL_LOCK`;
2. the parent turn's current model, when `SUBAGENT_INHERIT_MODEL` is true;
3. the child agent's `agent.yaml` `model:`;
4. the parent model as the fallback, also when `model: inherit`.

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
`model: inherit` and `reasoning: inherit` say the inheriting out loud: each
means the same as leaving the key out.
A child's `reasoning:` beats the effort it would inherit (the parent agent's,
or a resumed session's), never an EFFORT the operator gave by env, flag or
`/set`.
Invalid values fail agent loading rather than being silently ignored.

Top-level `js --agent <id>` also applies that agent's manifest through the same
route resolver. A one-shot `-p` run applies `model:` and `reasoning:` over the
jsrc files; `-m` / `--model`, `--reasoning`, a `JS_*` env var or `--extra` win
over the manifest, and a resumed session stays on its last stamp. The REPL
and `--bench` apply `model:` and `reasoning:`; `--commit` applies `model:`
only. In those three a configured non-default `MODEL` also leaves the
manifest model unused.

`SUBAGENT_MODEL_LOCK = true` (`Config.lock_subagent_model`) removes the `task`
tool's `model` parameter from both the model-facing description and the JSON
schema. The child can still use its own manifest `model:`; the parent model
just cannot override it through a tool call.

Do not expose raw endpoint URLs as normal model tool arguments unless the goal
is explicitly to let the model route traffic.
