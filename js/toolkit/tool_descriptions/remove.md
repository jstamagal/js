Move a file or directory to the trash.
{{#if undo}}
It is snapshotted first, so `undo` can bring it back.
{{/if}}
Symlinks are removed as symlinks, never followed. Anything over 512 MiB is
refused rather than trashed; `permanent=true` deletes outright, and only after
KING confirms.
