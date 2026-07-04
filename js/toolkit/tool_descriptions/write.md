Create a new file or overwrite an existing file.

Use this for new files, generated complete-file content, or replacing an entire
file intentionally.
{{#if patch}}
For existing source files, prefer `patch` so the edit stays narrow and
reviewable.
{{/if}}
{{#if multi_patch}}
For several replacements in one existing source file, prefer `multi_patch` so the
edit stays narrow and reviewable.
{{/if}}
{{#unless patch multi_patch}}
For existing source files, keep overwrites deliberate and complete; this surface
has no narrower edit tool.
{{/unless}}

Safety rules:
- Existing files require `overwrite=true`.
{{#if read}}
- Existing files also require a prior `read` of the same resolved path in the
  current process.
{{/if}}
{{#unless read}}
- Existing files also require a prior read snapshot in the current process; if no
  read tool is on this surface, overwriting an existing file may be rejected.
{{/unless}}
{{#if undo}}
- The tool snapshots prior bytes before overwriting so `undo` can restore the
  last in-process state.
{{/if}}
{{#unless undo}}
- The tool snapshots prior bytes internally before overwriting, but this surface
  has no separate undo tool; verify overwrites carefully.
{{/unless}}
- Existing newline style is preserved when overwriting; new files use normal
  UTF-8 text bytes.

When not to use:
{{#if patch}}
- Do not use this to make small edits to existing files. Use `patch`.
{{/if}}
{{#unless patch}}
- Do not use this to make small edits to existing files unless this surface has no
  narrower edit tool and a full overwrite is truly intended.
{{/unless}}
- Do not create documentation files (`*.md`) or README files unless the operator
  explicitly asked for durable documentation.
- Do not add emojis unless the operator explicitly asked for them.

Before writing:
- Make sure the parent directory is the intended location.
- If creating multiple related files, keep paths explicit and contents complete.
{{#if read}}
- If replacing an existing file, read it first and verify a complete overwrite is
  safer than a narrower edit.
{{/if}}
{{#unless read}}
- If replacing an existing file, verify a complete overwrite is safer than a
  narrower edit using the evidence this surface has.
{{/unless}}
