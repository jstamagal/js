Restore the latest in-process snapshot for a path.

Use this to recover from an incorrect file operation or when the operator asks
to revert a recent tool-made change.

Rules:
- Reverts the most recent `write`, `patch`, `multi_patch`, or `remove` snapshot
  for the resolved path.
- Works only within the current process.
- This is not a git reset and does not inspect repository history.
- Returns an error when no snapshot exists for the path.

Use carefully:
- Undo only the specific path that needs restoration.
- If multiple paths were changed, undo each path explicitly.
- After undoing, use `read` or `shell` with tests as appropriate to verify the
  workspace state.
