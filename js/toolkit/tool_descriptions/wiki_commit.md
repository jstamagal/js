Commit the current wiki vault state when the vault is a git repository.

Usage:
- Use before risky ingest runs or between explicit phases when a revert point matters.
- Empty commits are skipped.
- `message` should describe the vault change, not the agent's process.
