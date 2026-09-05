Search file contents by regex, or list files by name with `output_mode="files"`,
where `pattern` is a filename glob. Ripgrep underneath: paths ignored by
`.gitignore` and hidden paths are skipped unless passed as `path`; binary files
are skipped in content modes; empty directories are never listed. Results are
absolute paths, and `content` lines are `path:line:text`.
{{#if shell}}
For directory-only discovery use `shell` with `fd --type d`.
{{/if}}
