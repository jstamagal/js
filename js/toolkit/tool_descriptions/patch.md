Perform an exact string replacement in one file.

Use this for normal edits to existing files. It changes only the exact text you
name and returns a focused diff.
{{#if write}}
It is safer than `write` for narrow edits.
{{/if}}

Required preparation:
{{#if read}}
- Read the target file with `read` before editing.
{{/if}}
{{#unless read}}
- Patch requires the file to have been read earlier in this process. If this
  surface has no read tool and the file is not already snapshotted, patching may
  be rejected.
{{/unless}}
- The read-before-edit guard is path-based; read the same path you intend to
  edit after resolution.

Matching rules:
- `old_string` must match the file exactly, including whitespace and
  indentation.
- `new_string` must be different from `old_string`.
- The match must be unique unless `replace_all=true`.
- Use a larger `old_string` with surrounding context when the text appears more
  than once.
- Use `replace_all` only when every occurrence is intended to change, such as a
  safe rename within one file.

Aliases:
- `search` is accepted as an alias for `old_string`.
- `content` is accepted as an alias for `new_string`.

{{#if read}}
Working from `read` output:
- Text lines are returned as `12ab|content`.
- Preserve the exact indentation after the `|`.
- Never include the line number/hash prefix in `old_string` or `new_string`.
{{/if}}
{{#unless read}}
Working from prior file text:
- Preserve exact indentation and whitespace in `old_string`.
- Do not include line-number prefixes from any command output in `old_string` or
  `new_string`.
{{/unless}}

When not to use:
{{#if multi_patch}}
- Use `multi_patch` for several replacements in the same file.
{{/if}}
{{#unless multi_patch}}
- For several replacements in the same file, make each exact replacement
  deliberately; this surface has no batch-patch tool.
{{/unless}}
{{#if write}}
- Use `write` only when creating a new file or intentionally replacing the
  complete file.
{{/if}}
{{#unless write}}
- This tool edits existing files only; this surface has no create/overwrite tool.
{{/unless}}
{{#if shell}}
- Do not use `shell` with `sed`, `awk`, or redirection for edits.
{{/if}}
- Do not add emojis unless the operator explicitly asked for them.
