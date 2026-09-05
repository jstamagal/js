Create a file, or replace one whole.
{{#if read}}
Existing files need `overwrite=true` and a prior `read` of the whole file.
{{/if}}
{{#unless read}}
Existing files cannot be overwritten on this surface: that needs a prior read of
the whole file, and there is no read tool. Write new files only.
{{/unless}}
{{#if patch}}
Edit existing files with `patch`; use this only for new files or a deliberate
full rewrite.
{{/if}}
