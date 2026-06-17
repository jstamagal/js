Remove a file or directory after recording an undo snapshot.

Use this when deletion is explicitly part of the requested change or is clearly
needed to complete it.

Rules:
- `path` may be absolute, relative to the current working directory, or `~`
  expanded.
- Directories are removed recursively.
- Missing paths return an error.
- The most recent removal can be restored with `undo` in the same process.

Before deleting:
- Prefer `read`, `fs_search`, or `shell` when you need to verify what will be
  removed.
- Do not delete generated-looking, user-created, or unrelated files just to clean
  up a workspace.
- If the operator did not ask for deletion and the need is not obvious, ask with
  `followup`.
