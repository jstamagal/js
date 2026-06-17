Persist a markdown plan under `./plans`.

Use this for durable implementation plans, task breakdowns, investigation notes,
or strategy documents that should survive the current turn.

Rules:
- `plan_name` and `version` are sanitized into the filename.
- `content` is written verbatim as markdown.
- Existing plan files are snapshotted before overwrite.
- This is for plans, not source-code edits.

When to use:
- The operator asks for a plan to be written down.
- A complex implementation needs a durable checklist or design sketch.
- You need a persistent handoff artifact for future turns.

When not to use:
- Do not create documentation proactively.
- Do not use this for ordinary source edits; use `patch`, `multi_patch`, or
  `write`.
