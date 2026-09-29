Create a file, or replace one whole.
{{#if read}}
Existing files need `overwrite=true` and a prior `read` of the file; one page is
enough, the whole file is not needed. If the file changed on disk since that
read, the overwrite is refused; the error names both hashes and shows the diff
from what you read to what is there now, and a retry after it goes through.
{{/if}}
{{#unless read}}
Existing files cannot be overwritten on this surface: that needs a prior read of
the file, and there is no read tool. Write new files only.
{{/unless}}
{{#if undo}}
The replaced content is snapshotted, so `undo` brings it back.
{{/if}}
{{#if patch}}
Edit existing files with `patch`; use this only for new files or a deliberate
full rewrite.
{{/if}}
