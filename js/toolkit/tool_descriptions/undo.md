Restore the latest in-process snapshot for a path.

Use this to recover from an incorrect file operation or when the operator asks
to revert a recent tool-made change.

Rules:
- Reverts the most recent snapshot for the resolved path.
{{#if write}}
- Snapshots can come from `write`.
{{/if}}
{{#if patch}}
- Snapshots can come from `patch`.
{{/if}}
{{#if multi_patch}}
- Snapshots can come from `multi_patch`.
{{/if}}
{{#if remove}}
- Snapshots can come from `remove`.
{{/if}}
- Works only within the current process.
- This is not a git reset and does not inspect repository history.
- Returns an error when no snapshot exists for the path.

Use carefully:
- Undo only the specific path that needs restoration.
- If multiple paths were changed, undo each path explicitly.
{{#if read}}
- After undoing, use `read` to inspect the restored path.
{{/if}}
{{#if shell}}
- After undoing, use `shell` with tests as appropriate to verify the workspace
  state.
{{/if}}
{{#unless read shell}}
- After undoing, verify the restored path with whatever evidence this surface can
  return.
{{/unless}}
