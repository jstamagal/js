Edit a single file using exact text replacement. Every edits[].oldText must match a unique, non-overlapping region of the original file. If two changes affect the same block or nearby lines, merge them into one edit instead of emitting overlapping edits. Do not include large unchanged regions just to connect distant changes.

<!--parameters-->

### path

Path to the file to edit (relative or absolute)

### edits

One or more targeted replacements. Each edit is matched against the original file, not incrementally. Do not include overlapping or nested edits. If two changes touch the same block or nearby lines, merge them into one edit instead.

### edits.items.oldText

Exact text for one targeted replacement. It must be unique in the original file and must not overlap with any other edits[].oldText in the same call.

### edits.items.newText

Replacement text for this targeted edit.
