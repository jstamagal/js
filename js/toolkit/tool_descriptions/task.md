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
- Reading one known file: use `read`.
- Searching exact text, identifiers, or class/function definitions: use
  `fs_search`.
- Searching within one known file or two to three known files: use `read`
  directly.
- Simple tasks that you can complete with one or two direct tool calls.

Inputs:
- `tasks` is required and should contain clear, detailed, self-contained prompts.
- Each task is a string prompt.
- `agent_id` is required and selects the worker persona and selected tools.
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
  back to the model.

Prompting guidance:
- Include the expected output shape.
- Say whether code changes are allowed or whether the worker should only
  research.
- Include relevant paths, error text, constraints, and success criteria.
- Do not assume a fresh worker can see unspoken context; include what it needs
  unless resuming a session.
- If asking multiple workers to compare areas, make their scopes non-overlapping.

Failure behavior:
- One worker failure returns that worker's error without discarding sibling
  results.
- A task recursion limit prevents unbounded worker spawning.
- Workers are not exposed as controllable jobs; progress is visible only through
  streamed child tool activity and the final worker result.
