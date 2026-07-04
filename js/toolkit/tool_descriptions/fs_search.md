Search local text files with regular expressions, backed by ripgrep (`rg`).

This is the default tool for exact search tasks. Use it instead of running
`grep`, `rg`, `find`, `cat`, `head`, `tail`, `sed`, or `awk` through `shell`
when the goal is file discovery or content search.

Use for:
- Exact strings, identifiers, TODOs, filenames, literals, and regex patterns.
- Finding all occurrences of a variable, function, class, or phrase.
- Narrowing a search by directory, glob, or file extension.
- Getting matching file paths before reading the most relevant files.

When not to use:
{{#if sem_search}}
- Use `sem_search` for intent-based exploration when you do not know the
  exact words.
{{/if}}
{{#if read}}
- Use `read` when you already know the file path and need file contents.
{{/if}}
{{#if task}}
- Use `task` for open-ended investigations that require multiple search/read
  rounds or synthesis across several areas.
{{/if}}

Pattern behavior:
- `pattern` is a ripgrep (Rust regex) regular expression.
- Literal braces and other regex metacharacters must be escaped when you mean
  them literally, for example `interface\{\}`.
- By default, each line is matched on its own.
- Set `multiline=true` for patterns that must span line breaks; in that mode `.`
  also matches newlines.
- Use `case_insensitive=true` for case-insensitive search.

What is searched:
- Respects `.gitignore` inside a git repository, and `.ignore` / `.rgignore`
  files anywhere. Ignored paths are not searched.
- Hidden files and directories (dot-prefixed) are skipped; pass an explicit
  `path` to a hidden file to search it directly.
- Binary and non-regular files (pipes, sockets, devices) are skipped.

Filtering:
- `path` may be a file or directory and defaults to the current working
  directory. Results are absolute paths.
- `glob` filters by ripgrep glob such as `*.py` or `**/*.tsx`.
- `file_type` filters by extension without the dot, such as `py`, `js`, or `rs`.

Output modes:
- `files_with_matches` returns only paths and is the default.
- `content` returns matching lines as `path:line:text`.
- `count` returns per-file match counts as `path:count`.
- `before_context`, `after_context`, and `context_lines` apply only to
  `content` output.
- `head_limit` limits returned entries after `offset`.
