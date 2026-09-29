Replace exact text in one file.
{{#if read}}
Every line `old_string` touches must have been shown by a prior `read`. The
`12|` prefix in read output is not file content; only the text after `|` is.
If the file changed on disk since your read, the call writes nothing and the
error shows the diff from what you read to what is there now; retry against the
current text without reading again. Lines the diff shows count as read.
{{/if}}
{{#unless read}}
Every line `old_string` touches must have been shown by a prior read, and this
surface has no read tool, so edits will be refused.
{{/unless}}
`old_string` must match exactly once unless `replace_all` is set. When it is
not in the file exactly, it is matched again with smart quotes, Unicode dashes,
no-break spaces and trailing whitespace read as plain ASCII; the file keeps its
own characters wherever your edit left `old_string` unchanged. Pass `edits`
for several replacements in one call: they apply in order, each seeing the
previous result, and all are validated before anything is written, so a failed
call leaves the file untouched and names the offending edit.
{{#if undo}}
One call is one `undo` step.
{{/if}}
{{#if write}}
Use `write` for new files or a deliberate full rewrite.
{{/if}}
