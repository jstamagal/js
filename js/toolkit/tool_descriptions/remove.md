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
{{#if read}}
- Use `read` when you need to inspect a known file before deleting it.
{{/if}}
{{#if fs_search}}
- Use `fs_search` when you need to find or verify deletion targets by name or
  content.
{{/if}}
{{#if shell}}
- Use `shell` with safe listing commands when this surface lacks a narrower tool
  for verifying what will be removed.
{{/if}}
- Do not delete generated-looking, user-created, or unrelated files just to clean
  up a workspace.
{{#if followup}}
- If the operator did not ask for deletion and the need is not obvious, ask with
  `followup`.
{{/if}}
{{#unless followup}}
- If the operator did not ask for deletion and the need is not obvious, stop and
  ask in plain text before deleting.
{{/unless}}
