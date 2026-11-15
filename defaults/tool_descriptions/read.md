Read one file: text, PDF, image, or notebook. PDFs come back as extracted text,
images as images when the model has vision. A notebook (.ipynb) comes back as
its cells, each with its id, source and a summary of its outputs; edit cells
with notebook_edit. A line or byte range reads a notebook's raw JSON. Text comes
back as `12|content`: line number, then the text. Lines are returned whole,
however long they are. The prefix is not file content; strip it when you quote
text back into an edit. Reading the same lines again while the file is
unchanged returns a short note naming the earlier read call whose result still
shows them.

A long file stops at a line limit and says how to continue; pass `start_line`
and optional `end_line` (inclusive, 1-based) for the rest. A file too large to
read whole is refused with the ranged call to use instead; a line range of it
is read line by line. `start_byte` (0-based offset) and optional `end_byte`
(one past the last byte) read a byte range, for text with few or no line
breaks: it returns raw text without line prefixes, one page at a time, and
names the offset to continue from. Given both a line range and a byte range,
the line range is read and the result says the byte range was not. A nested
`range` object with the same fields, `offset`/`limit` (first line, line count)
and `line_start`/`line_end` are read as line ranges too. A tool result too
large to show inline is saved to a file; its notice names the path and the
range that continues it. A path that does not exist is refused with the
nearest existing directory, close names in its parent directory, and existing
paths that end the same way. When js runs with `-C DIR`, a path outside DIR and
the paths the operator bound is refused with one ERROR line.
{{#if skill tool_discovery}}
The first call that touches a file a skill's `paths:` names starts its result
with a `<js-reminder>` offering that skill, once per session. The reminder is
not file content.
{{/if}}
{{#if fs_search}}
Find files and search contents with `fs_search`. This reads a known path.
{{/if}}
