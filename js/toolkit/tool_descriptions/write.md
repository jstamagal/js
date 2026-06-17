Create a new file or overwrite an existing file.

Use this for new files, generated complete-file content, or replacing an entire
file intentionally. For existing source files, prefer `patch` or `multi_patch`
so the edit stays narrow and reviewable.

Safety rules:
- Existing files require `overwrite=true`.
- Existing files also require a prior `read` of the same resolved path in the
  current process.
- The tool snapshots prior bytes before overwriting so `undo` can restore the
  last in-process state.
- Existing newline style is preserved when overwriting; new files use normal
  UTF-8 text bytes.

When not to use:
- Do not use this to make small edits to existing files. Use `patch`.
- Do not create documentation files (`*.md`) or README files unless the operator
  explicitly asked for durable documentation.
- Do not add emojis unless the operator explicitly asked for them.

Before writing:
- Make sure the parent directory is the intended location.
- If creating multiple related files, keep paths explicit and contents complete.
- If replacing an existing file, read it first and verify a complete overwrite is
  safer than a patch.
