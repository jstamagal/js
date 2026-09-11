Archive an ingested unit, append the log entry, and commit the vault when possible.

Usage:
- Use for normal ingest completion instead of separate archive and log calls.
- Archive runs first; if it fails, no log entry or commit is created.
- The commit is skipped when the vault is not a git repo or there is no diff.
- The commit runs only when the vault is the root of its git work tree; a vault
  nested inside another repository is refused rather than committed there.
- A failed commit comes back as an error, with the archive and log left in place
  to retry.
- `note` should summarize what was added or why the unit was skipped.
