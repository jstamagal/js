Restore one path to its latest snapshot. Every tool that changes a file
snapshots it first.
{{#if patch}}
One multi-edit `patch` call is one snapshot.
{{/if}}
Each call pops one snapshot, so call again to step back further. Snapshots
survive a restart of a saved session. This is not git and does not touch
repository history.
