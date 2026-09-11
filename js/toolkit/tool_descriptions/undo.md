Restore one path to its latest snapshot. Every tool that changes a file
snapshots it first.
Restores file/directory permission modes and symlink targets within removed
trees. Ownership, timestamps, and extended attributes are not restored; older
snapshots without modes restore content only.
{{#if patch}}
One multi-edit `patch` call is one snapshot.
{{/if}}
Each successful restore pops one snapshot; filesystem errors retain it for
retry. An unusable snapshot (including failed capture) is discarded with an
error without changing the path; call again to reach an older entry. Snapshots
survive a restart of a saved session. This is not git and does not touch
repository history.
