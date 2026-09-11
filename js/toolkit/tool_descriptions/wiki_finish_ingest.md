Archive an ingested unit, append the log entry, and commit the vault when possible.

Usage:
- Use for normal ingest completion instead of separate archive and log calls.
- Archive runs first; if it fails, no log entry or commit is created.
- The commit is skipped when the vault is not a git repo or there is no diff.
- `note` should summarize what was added or why the unit was skipped.
