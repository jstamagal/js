Trash or remove a file/directory after recording an undo snapshot.

Default behavior:
- Targets up to 512 MiB go to `trash` / `trash-put`.
- Targets over 512 MiB stop instead of filling trash.
- `permanent=true` removes directly; only use it after KING confirms direct deletion.

Rules:
- `path` may be absolute, relative to the current working directory, or `~`
  expanded.
- Directories are handled recursively.
- Symlinks are removed as symlinks; `remove` does not follow them.
- Missing paths return an error.


Before removing:
- Prefer `read`, `fs_search`, or `shell` when you need to verify what will be
  removed.
- Do not delete generated-looking, user-created, or unrelated files just to clean
  up a workspace.
- If the operator did not ask for deletion and the need is not obvious, ask with
  `followup`.
