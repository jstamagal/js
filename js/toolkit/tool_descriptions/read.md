Read one file: text, PDF, image, or notebook. PDFs come back as extracted text,
images as images when the model has vision, notebooks as raw JSON. Text comes
back as `12|content`: line number, then the text. Lines are returned whole,
however long they are. The prefix is not file content; strip it when you quote
text back into an edit. Reading the same lines again while the file is
unchanged returns a short note naming the earlier read call whose result still
shows them.

A long file stops at a line limit and says how to continue; use `range` for the
rest. `range` also takes a byte range, `start_byte` (0-based offset) and
optional `end_byte` (one past the last byte), for text with few or no line
breaks: it returns raw text without line prefixes, one page at a time, and
names the offset to continue from. A tool result too large to show inline is
saved to a file; its notice names the path and the `range` that continues it.
When js runs with `-C DIR`, a path outside DIR and the paths the operator bound
is refused with one ERROR line.
{{#if fs_search}}
Find files and search contents with `fs_search`. This reads a known path.
{{/if}}
