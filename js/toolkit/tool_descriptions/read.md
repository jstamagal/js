Read one file. Text comes back as `12:ab|content`: line number, short hash of
the line, then the text. Lines are returned whole, however long they are. The
prefix is not file content; strip it when you quote text back into an edit.

A long file stops at a line limit and says how to continue; use `range` for the
rest. Images come back as images when the model has vision, PDFs as extracted
text, notebooks as raw JSON.
{{#if fs_search}}
Find files and search contents with `fs_search`. This reads a known path.
{{/if}}
