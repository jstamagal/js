Create, update, or remove the current in-process task list.

Use this to track progress during non-trivial coding sessions. It helps prevent
missed requirements and makes multi-step work explicit.

How it works:
- Each item has `content` and `status`.
- `content` is the unique key.
- Sending a new `content` adds a task.
- Sending existing `content` updates that task's status.
- Items not mentioned are left unchanged.
- `status=cancelled` removes the item entirely.

Valid statuses:
- `pending`: not started.
- `in_progress`: currently being worked on.
- `completed`: fully done.
- `cancelled`: no longer relevant; remove from the list.

When to use:
- Complex tasks with three or more meaningful steps.
- Requests involving multiple files, subsystems, or verification phases.
- User-provided task lists.
- After receiving new requirements that must not be lost.
- Before starting a multi-step task, mark exactly one item `in_progress`.
- Immediately after finishing a tracked task, mark it `completed`.

When not to use:
- A single straightforward action.
- Trivial conversational or informational answers.
- Work that can be completed in less than three simple steps.

Completion discipline:
- Do not mark a task `completed` until it is fully accomplished.
- Do not mark complete if tests are failing, implementation is partial, or a
  blocker remains.
- Keep at most one task `in_progress` at a time.
- If blocked, keep the task active and add or report the blocker.
- Send only changed items; do not repeat unchanged tasks.
