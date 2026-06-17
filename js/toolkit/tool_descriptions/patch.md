Perform an exact string replacement in one file.

Use this for normal edits to existing files. It is safer than `write` because
it changes only the exact text you name and returns a focused diff.

Required preparation:
- Read the target file with `read` before editing.
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

Working from `read` output:
- Text lines are returned as `12ab|content`.
- Preserve the exact indentation after the `|`.
- Never include the line number/hash prefix in `old_string` or `new_string`.

When not to use:
- Use `multi_patch` for several replacements in the same file.
- Use `write` only when creating a new file or intentionally replacing the
  complete file.
- Do not use `shell` with `sed`, `awk`, or redirection for edits.
- Do not add emojis unless the operator explicitly asked for them.
