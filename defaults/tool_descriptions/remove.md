Move a file or directory to the trash, or delete it with `permanent=true`.
{{#if undo}}
Both are snapshotted first, so `undo` can bring the path back, except a path over
512 MiB.
{{/if}}
Symlinks are removed as symlinks, never followed. `permanent=true` does not use
the trash at all: when the trash is missing or fails, it is the way to remove the
path. Anything over 512 MiB is refused rather than trashed; confirm with the
operator before deleting that with `permanent=true`, which cannot be undone.

<!--parameters-->

### path

File or directory path to delete.

### permanent

Delete directly without the trash; the path is still snapshotted for undo.
