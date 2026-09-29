Launch one or more delegated worker agent turns for complex, multi-step work.

Use this when the work benefits from isolated investigation, parallel research,
or a specialized agent persona. Each worker runs autonomously and returns a
single compressed result back to the parent turn. The worker's result is not
shown directly to the operator; you must synthesize it in your final response.

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
- Non-task tools run sequentially.
- Results are restored to the original task/tool-call order before being sent
  back to the model. One task returns the worker's reply verbatim; a fan-out
  returns them numbered under a `TASK_RESULTS` header.

Background:
- A call blocks until every worker finishes. With `background=true` it returns
  at once with a handle instead, and the workers keep running while you do
  other work:

      task running in the background (handle t1): ...
      HANDLE t1 RUNNING

- `action="poll", handle="t1"` says whether it is still running and returns the
  result once it is done. `action="wait", handle="t1", timeout=N` blocks up to
  N seconds, or until the end without `timeout`. `action="kill", handle="t1"`
  stops it. `handle` defaults to the most recent running task. `tasks` and
  `agent_id` are needed only to start one.
- The finished result is also written to the file the handle names. If you have
  not polled it by then, the next user message carries a `<js-reminder>` that
  names that file.
- In a one-shot run (`js -p`) a background task still running when the run ends
  is stopped with it; wait for it before your final answer.
- Use it for slow, independent work you do not need before your next step.
  When the next step depends on the result, run it in the foreground.

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
- A foreground call is not a controllable job; its progress is visible only
  through streamed child tool activity and the final worker result.
