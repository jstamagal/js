Read a local file from disk.

Use this whenever you know the specific file path and need its contents. If the
operator provides a path, assume it is worth trying; nonexistent paths return a
tool error.

Path and range rules:
- `file_path` may be absolute, relative to the current working directory, or `~`
  expanded.
- By default, text reads start at line 1 and return at most the configured
  `ToolContext.max_read_lines`.
- Use `range.start_line` and `range.end_line` for long files. Prefer reading a
  useful large section over many tiny reads.
- Lines longer than `ToolContext.max_line_chars` are truncated in the returned
  output.
- `show_line_numbers=false` returns plain selected text.

Text output format:
- Text files return anchored lines as `12ab|content`.
- `12` is the 1-based line number.
- `ab` is a short hash of the displayed line, useful for stable references after
  truncation.
- Do not include the `12ab|` prefix when passing exact text to `patch` or
  `multi_patch`; only the content after `|` belongs to the file.

Visual and binary handling:
- Images (`png`, `jpg`, `jpeg`, `webp`, `gif`) return visual content when
  `JS_VISION` or the configured model enables vision.
- If vision is disabled, image reads return a text fallback with path, MIME type,
  and byte size.
- PDF files are converted through `pdftotext` and returned as text. Scanned PDFs
  report conversion failure rather than pretending OCR happened.
- Jupyter notebooks (`.ipynb`) are read as plain JSON text.

When not to use:
{{#if fs_search}}
- This tool reads files only. Use `fs_search` or `shell` when you need directory
  discovery.
- Use `fs_search` for exact string, regex, or broad content discovery.
{{/if}}
{{#unless fs_search}}
- This tool reads files only. Use `shell` (`fd` to find files, `rg` to search
  their contents) when you need directory discovery or broad content search.
{{/unless}}
{{#if sem_search}}
- Use `sem_search` for intent-based exploration across unfamiliar code.
{{/if}}

Batching:
- When several specific files may be relevant, request them in the same
  assistant turn so the runtime can return all results together.
