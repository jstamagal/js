Create a file, or replace one whole.
{{#if read}}
Replacing an existing file needs a prior `read` of it; one page is enough, the
whole file is not needed. A read earlier in this session counts
after js restarts on it. If the file changed on disk since that
read, the overwrite is refused; the error names both hashes and shows the diff
from what you read to what is there now, and a retry after it goes through.
Passing `expected_hash` (the hash a read or write result printed) replaces the
read: a match overwrites, a mismatch names both hashes and writes nothing.
{{/if}}
{{#unless read}}
Existing files cannot be overwritten on this surface: that needs a prior read of
the file, and there is no read tool. Write new files only.
{{/unless}}
{{#if undo}}
The replaced content is snapshotted, so `undo` brings it back.
{{/if}}
When js runs with `-C DIR`, a path outside DIR and the writable paths the
operator bound is refused with an ERROR.
A failed write keeps its content: the ERROR names a held id (`h3`) and the
file holding it. Finish with `{"file_path": ..., "from_held": "h3"}` instead of
sending the content again; the same checks apply, and the held content is
deleted once written. A call cut off mid-content holds what arrived; add the
rest with `from_held`, `append: true` and `content` holding only the rest.
`append: true` without `from_held` adds `content` to the end of the file.
{{#if skill tool_discovery}}
The first call that touches a file a skill's `paths:` names starts its result
with a `<js-reminder>` offering that skill, once per session. The reminder is
not file content.
{{/if}}
{{#if patch}}
Edit existing files with `patch`; use this only for new files or a deliberate
full rewrite.
{{/if}}
