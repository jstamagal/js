Replace exact text in one file.
{{#if read}}
An `old_string` of 24 characters or more that matches the file exactly needs
no prior `read`: the match is the proof. A shorter or inexact `old_string` is
applied only to lines a prior `read` showed. The `12|` prefix in read output is
not file content; only the text after `|` is. If such an edit misses and the
file changed on disk since your read, the error shows the diff from what you
read to what is there now; retry against the current text without reading
again. Lines the diff shows count as read.
{{/if}}
{{#unless read}}
This surface has no read tool: only an `old_string` of 24 characters or more
that matches the file exactly can be edited. Shorter or inexact matches are
refused.
{{/unless}}
`old_string` must match exactly once unless `replace_all` is set. When it is
not in the file exactly, it is matched again with smart quotes, Unicode dashes,
no-break spaces and trailing whitespace read as plain ASCII; the file keeps its
own characters wherever your edit left `old_string` unchanged. With
`replace_all`, this fallback runs only when there is no exact occurrence. Pass `edits`
for several replacements in one call: they apply in order, each seeing the
previous result, and all are validated before anything is written, so a failed
call leaves the file untouched and names the offending edit. An edit whose
`old_string` equals its `new_string` changes nothing: it is left out, the rest
apply, and the result names it; when every edit is one, nothing is written. A
path that does not exist is refused with the nearest existing directory, close
names in its parent directory, and existing paths that end the same way.
{{#if undo}}
One call is one `undo` step.
{{/if}}
{{#if skill tool_discovery}}
The first call that touches a file a skill's `paths:` names starts its result
with a `<js-reminder>` offering that skill, once per session. The reminder is
not file content.
{{/if}}
{{#if write}}
Use `write` for new files or a deliberate full rewrite.
{{/if}}

<!--parameters-->

### edits

Several replacements applied in order before one final write. Use instead of old_string/new_string, never alongside them.

### edits.items.new_string

Replacement text.

### edits.items.old_string

Exact text to replace.

### edits.items.replace_all

Replace every occurrence of this edit.

### file_path

File path to edit.

### new_string

Replacement text.

### old_string

Exact text to replace. Pair with new_string, or use edits instead.

### params.edits

Several replacements validated and applied in order before one final write. Use instead of old_string/new_string, never alongside them.

### params.edits.items.new_string

Replacement text.

### params.edits.items.old_string

Exact text to replace.

### params.edits.items.replace_all

Replace every occurrence of this edit.

### params.file_path

File path to edit.

### params.new_string

Replacement text. Required unless edits is used.

### params.old_string

Exact text to replace. Required unless edits is used.

### params.replace_all

Replace every occurrence instead of requiring one unique match.

### replace_all

Replace every occurrence instead of requiring one unique match.
