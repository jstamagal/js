Launch one or more delegated worker agent turns for complex, multi-step work.

Use this when the work benefits from isolated investigation, parallel research,
or a specialized agent persona. Each worker runs autonomously and returns a
single compressed result back to you. The worker's result is not shown
directly to the operator; you must synthesize it in your final response.

When to use:
- Open-ended searches that may require multiple search/read rounds.
- Independent investigations that can happen concurrently.
- Specialized work where `agent_id` should load another persona and tool
  surface.
- Verification, research, or codebase exploration that is separable from your
  main editing path.

When not to use:
{{#if read}}
- Reading one known file: use `read`.
- Searching within one known file or two to three known files: use `read`
  directly.
{{/if}}
{{#unless read}}
- Reading one known file is not enough reason to delegate; use the tools on this
  surface directly when possible.
{{/unless}}
{{#if fs_search}}
- Searching exact text, identifiers, or class/function definitions: use
  `fs_search`.
{{/if}}
{{#unless fs_search}}
- Searching exact text, identifiers, or class/function definitions is not enough
  reason to delegate by itself; use a local search path available on this
  surface when possible.
{{/unless}}
- Simple tasks that you can complete with one or two direct tool calls.

Inputs:
- `tasks` is required to start workers and should contain clear, detailed,
  self-contained prompts.
- Each task is a string prompt.
- `agent_id` is required to start workers and selects the worker persona and
  selected tools.
- Workers inherit the parent turn's effective configuration and shared instruction
  files; worker persona and model-selection rules still apply.
- `session_id` resumes a worker session. When resumed, the worker keeps previous
  context. When omitted, a fresh worker session is created.
- `tasks` is what the worker reads. The worker's routing — model, agent_id,
  session_id — rides the fields, never the prose.
<!--if:model_override-->
- `model` overrides the model the worker runs on. Leave it unset by default — the
  worker uses its configured model. ONLY set it when the operator explicitly asks
  for behavior the agent isn't configured for; do not pick a model on your own.
<!--endif-->

Parallelism:
- Multiple tasks inside one `task` call run concurrently with a bounded worker
  pool.
- Multiple separate `task` tool calls emitted in one assistant turn also run in
  parallel at the runtime orchestration layer.
- Other tools run in model order: consecutive read-only calls run together,
  and any other call runs alone, after the calls before it.
- Results are restored to the original task/tool-call order before being sent
  back to the model. One task returns the worker's reply verbatim; a fan-out
  returns them numbered under a `TASK_RESULTS` header.

Running, watching, stopping and talking to workers:
- A call starts its workers and returns their handles at once; they run while
  you keep working or answer the operator:

      task t1 started: 2 subagents of agent `explore` (t1.1, t1.2), ...
      HANDLE t1 RUNNING

  `t1` is the task, `t1.1` and `t1.2` its workers. `background=false` waits
  for the workers instead and returns their result; do that when your very
  next step needs it. Inside a subagent a call waits unless you pass
  `background=true`.
- `action="list"`: your tasks and each worker's state.
- `action="poll", handle="t1"`: whether it is still running, and for each
  worker its state, the tools it has called, the text it last wrote and its
  session file; the result once it is done. `handle="t1.2"` shows one worker.
- `action="send", handle="t1.2", message="..."`: talk to a worker. A running
  one reads it at its next tool call, or answers it in a further turn before
  it finishes when it makes none. A finished one answers it in a new turn
  on its own session, with everything it did before, started as a new task
  whose handle the call returns.
- `action="wait", handle="t1", timeout=N`: block up to N seconds, or until it
  ends without `timeout`.
- `action="kill", handle="t1"`: stop the task. `handle="t1.2"` stops that
  worker alone; the others carry on.
- `handle` defaults to your most recent running task. You see only the tasks
  you started; `tasks` and `agent_id` are needed only to start one.
- When a task you have not polled finishes, its result comes to you in a
  `<js-reminder>`: at your next tool call, with the next user message, or, if
  you are idle, in a message of its own. It is also written to the file the
  start line names. Do not poll in a loop to wait for it; carry on, or `wait`.

Prompting guidance:
- Include the expected output shape.
- Say whether code changes are allowed or whether the worker should only
  research.
- Include relevant paths, error text, constraints, and success criteria.
- Do not assume a fresh worker can see unspoken context; include what it needs
  unless resuming a session.
- If asking multiple workers to compare areas, make their scopes non-overlapping.

Failure behavior:
- A missing agent returns an error naming it, without starting a worker or
  creating/resuming its session.
- One worker failure returns that worker's error without discarding sibling
  results.
- A task recursion limit prevents unbounded worker spawning.
- A call with `background=false` is not a controllable task; its progress is
  visible only through streamed child tool activity and the final result.

<!--parameters-->

### action

run starts workers; list, poll, wait, kill and send act on your tasks by handle.

### agent_id

Worker agent id; loads that agent's persona and selected tools.

### background

true (the default for the main agent): return the handles at once while the workers run. false: wait for their result.

### handle

A task (t1) or one of its workers (t1.2), for poll, wait, kill and send.

### message

What send says to the worker.

### model

Model for the subagent(s), same string as --model. Set it when the operator names one; omit otherwise.

### session_id

Optional session id for a worker context.

### tasks

One or more clear, detailed task prompts to run in parallel.

### timeout

Seconds a wait blocks; without it, wait blocks until the task ends.
