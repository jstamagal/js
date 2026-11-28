Create, append to, or replace a file.
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
Use this surface to create files or append to them.
{{/unless}}
{{#if undo}}
The replaced content is snapshotted, so `undo` brings it back.
{{/if}}
When js runs with `-C DIR`, a path outside DIR and the writable paths the
operator bound is refused with an ERROR.
A failed write keeps its content: the ERROR names a held id (`h3`) and the
file holding it. Finish with `{"file_path": ..., "from_held": "h3"}` instead of
sending the content again. For an unread existing file, read it first. The
held content is deleted once written. A call cut off mid-content holds what
arrived. Add the rest with `from_held`, `append: true` and `content` holding only the rest.
`append: true` without `from_held` adds `content` to the end of the file without a prior read.
{{#if skill tool_discovery}}
The first call that touches a file a skill's `paths:` names starts its result
with a `<js-reminder>` offering that skill, once per session. The reminder is
not file content.
{{/if}}
{{#if patch}}
Use `patch` to replace parts of an existing file.
{{/if}}

<!--parameters-->

### append

Add content to the end of the file without a prior read. With from_held, content is added after the held text and the result written whole.

### content

File content to write or text to append.

### expected_hash

The file's current hash, as a read or write result printed it. A match permits an overwrite without a prior read. An append checks the hash if supplied.

### file_path

File path to create, append to, or overwrite.

### from_held

A held id (h3) from a failed write's ERROR: write the held content instead of sending it again. With append=true, content is added after it.

### overwrite

Set it when a refused write asks for it.
