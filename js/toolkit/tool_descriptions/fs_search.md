Search file contents by regex, or list files by name with `output_mode="files"`,
where `pattern` is a filename glob. Ripgrep underneath: paths ignored by
`.gitignore` are skipped, as are hidden paths unless named by `path` or by a
dot-leading pattern/glob such as `.env`; binary files are skipped in content
modes; empty directories are never listed. Results are
absolute paths, and `content` lines are `path:line:text`. Bytes that are not
valid UTF-8 are shown as `\xNN` escapes.
{{#if shell}}
For directory-only discovery use `shell` with `fd --type d`.
{{/if}}
{{#if task}}
- Use `task` for open-ended investigations that require multiple search/read
  rounds or synthesis across several areas.
{{/if}}
