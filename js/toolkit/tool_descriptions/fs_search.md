Search local file contents with regular expressions or discover files by name,
backed by ripgrep (`rg`).

This is the default tool for exact search tasks.
{{#if shell}}
Use it instead of running `grep` or `rg` through `shell` for content search, and
instead of `fd`, `find`, or `rg --files` for file discovery by name.
{{/if}}
{{#unless shell}}
Use it for file discovery by name or content search; this surface has no
terminal-tool fallback for listing directories themselves.
{{/unless}}

Use for:
- Exact strings, identifiers, TODOs, literals, and content regex patterns.
- Locating files with a filename glob such as `settings.py` or `**/*.toml`.
- Finding all occurrences of a variable, function, class, or phrase.
- Narrowing a search by directory, glob, or file extension.
- Getting matching file paths before reading the most relevant files.

When not to use:
{{#if read}}
- Use `read` when you already know the file path and need file contents.
{{/if}}
{{#if task}}
- Use `task` for open-ended investigations that require multiple search/read
  rounds or synthesis across several areas.
{{/if}}

Pattern behavior:
- In `files_with_matches`, `content`, and `count` modes, `pattern` is a ripgrep
  (Rust regex) regular expression matched against file contents, not filenames.
- Literal braces and other regex metacharacters must be escaped when you mean
  them literally, for example `interface\{\}`.
- By default, each line is matched on its own.
- Set `multiline=true` for patterns that must span line breaks; in that mode `.`
  also matches newlines.
- Use `case_insensitive=true` for case-insensitive search.
- In `files` mode, `pattern` is instead a filename/path glob passed to
  `rg --files`, and `case_insensitive=true` makes that glob case-insensitive.

What is searched:
- Respects `.gitignore` inside a git repository, and `.ignore` / `.rgignore`
  files anywhere. Ignored paths are not searched.
- Hidden files and directories (dot-prefixed) are skipped; pass an explicit
  `path` to a hidden file to search it directly.
- Content-search modes skip binary and non-regular files (pipes, sockets,
  devices). `files` may list binary files but still skips non-regular paths.
- `files` discovers regular files, including the paths needed to understand a
  tree. It does not report empty directories.
{{#if shell}}
- Use `shell` with `fd --type d` for directory-only discovery.
{{/if}}
{{#unless shell}}
- This surface has no tool for discovering empty directories.
{{/unless}}

Filtering:
- `path` may be a file or directory and defaults to the current working
  directory. Results are absolute paths.
- `glob` filters by ripgrep glob such as `*.py` or `**/*.tsx`.
- `file_type` (alias `type`) takes a ripgrep type name such as `rust`, `py`, or
  `js`, which covers every extension in that type. A bare extension ripgrep has
  no type for, such as `gdshader`, is matched as `*.gdshader` instead.
- The short ripgrep spellings are accepted for the flags below: `-B`, `-A`, `-C`,
  `-n`, `-i`. Either spelling works; the short one wins if both are sent.

Output modes:
- `files` returns paths whose filenames match the `pattern` glob without reading
  file contents.
- `files_with_matches` returns paths whose contents match and is the default.
- `content` returns matching lines as `path:line:text`.
- `count` returns per-file match counts as `path:count`.
- `before_context`, `after_context`, and `context_lines` apply only to
  `content` output.
- `head_limit` limits returned entries after `offset`.
