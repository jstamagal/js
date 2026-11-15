Move a file or directory to the trash, or delete it with `permanent=true`.
{{#if undo}}
Both are snapshotted first, so `undo` can bring the path back.
{{/if}}
Symlinks are removed as symlinks, never followed. `permanent=true` does not use
the trash at all: when the trash is missing or fails, it is the way to remove the
path. Anything over 512 MiB is refused rather than trashed; confirm with the
operator before deleting that with `permanent=true`.
