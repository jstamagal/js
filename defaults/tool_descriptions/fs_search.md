Search file contents by regex, or list files by name with `output_mode="files"`,
where `pattern` is a filename glob. Ripgrep underneath: paths ignored by
`.gitignore` are skipped, as are hidden paths unless named by `path` or by a
dot-leading pattern/glob such as `.env`; binary files are skipped in content
modes; empty directories are never listed. A positive `glob` (or `pattern` in
files mode) is a ripgrep whitelist that overrides those ignore rules, so it can
return an ignored file; a negated glob only filters. `file_type` and a positive
glob cannot be intersected, so passing both is rejected. Results are absolute
paths, and `content` lines are `path:line:text`. Bytes that are not valid UTF-8
are shown as `\xNN` escapes. The walk stays on the filesystem `path` is on and
does not enter mount points below it; to search a mounted share, name it as
`path`. A `path` that does not exist is refused with the nearest existing
directory, close names in its parent directory, and existing paths that end
the same way.
{{#if shell}}
For directory-only discovery use `shell` with `fd --type d`.
{{/if}}
{{#if task}}
- Use `task` for open-ended investigations that require multiple search/read
  rounds or synthesis across several areas.
{{/if}}

<!--parameters-->

### after_context

Lines after each match when output_mode is content.

### before_context

Lines before each match when output_mode is content.

### case_insensitive

Match without case sensitivity.

### context_lines

Lines before and after each match when output_mode is content.

### file_type

ripgrep type name or bare extension, e.g. rust, py, rs. Cannot be combined with a positive glob.

### glob

Optional glob filter such as *.py or **/*.tsx.

### head_limit

Maximum number of result entries after offset; must be at least 1.

### multiline

Allow the regex to span line breaks.

### offset

Number of result entries to skip before returning output.

### output_mode

Result format: filename-glob paths, content-matching lines, paths whose contents match, or per-file content-match counts.

### path

File or directory to search; defaults to the current working directory.

### pattern

Content regular expression, or filename glob when output_mode is files.

### show_line_numbers

Include file:line prefixes for content output.
