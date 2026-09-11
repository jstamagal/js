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

Keep project documentation current as work progresses: record what changed, how it works, and how to use it. Document new features and changed behavior without waiting for a separate request. Update relevant existing documentation; create a new document when the topic needs its own home.
