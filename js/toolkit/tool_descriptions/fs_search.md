Search local text files with regular expressions.

This is the default tool for exact search tasks. Use it instead of running
`grep`, `rg`, `find`, `cat`, `head`, `tail`, `sed`, or `awk` through `shell`
when the goal is file discovery or content search.

Use for:
- Exact strings, identifiers, TODOs, filenames, literals, and regex patterns.
- Finding all occurrences of a variable, function, class, or phrase.
- Narrowing a search by directory, glob, or file extension.
- Getting matching file paths before reading the most relevant files.

When not to use:
- Use `sem_search` for intent-based exploration when you do not know the
  exact words.
- Use `read` when you already know the file path and need file contents.
- Use `task` for open-ended investigations that require multiple search/read
  rounds or synthesis across several areas.

Pattern behavior:
- `pattern` is a regular expression.
- Literal braces and other regex metacharacters must be escaped when you mean
  them literally, for example `interface\{\}`.
- By default, patterns match within single lines.
- Set `multiline=true` only for patterns that must span line breaks.
- Use `case_insensitive=true` for case-insensitive search.

Filtering:
- `path` may be a file or directory and defaults to the current working
  directory.
- `glob` filters by path glob such as `*.py` or `**/*.tsx`.
- `file_type` filters by extension-like type such as `py`, `js`, or `rs`.
- Binary and visual files are skipped.

Output modes:
- `files_with_matches` returns only paths and is the default.
- `content` returns matching lines.
- `count` returns per-file match counts.
- `before_context`, `after_context`, and `context_lines` apply only to
  `content` output.
- `head_limit` limits returned entries after `offset`.
