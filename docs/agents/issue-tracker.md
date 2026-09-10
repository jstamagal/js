# Issue tracker: GitHub

The shared issue tracker is https://github.com/jstamagal/js/issues. Use `gh`
with `--repo jstamagal/js` so the destination is explicit. Local `.scratch/`
files are working notes and reproduction artifacts; leaving a report there
does not publish an issue or make it available on the other machines.

## Conventions

- Check open and closed issues and related PRs before filing a duplicate.
- Verify bug reports against the target branch. Include the tested commit,
  minimal reproduction, expected and actual behavior, and useful evidence.
- Put enough context in the GitHub issue for another machine to act on it;
  a local path alone is not a reproduction. Keep credentials out of reports.
- Use GitHub labels for category and triage state (see `triage-labels.md`).
- After a scratch report's confirmed issues are filed with self-contained
  evidence, delete the original report so it is not repeatedly triaged. For a
  combined audit, account for every finding before retiring the report. Commit
  tracked deletions so a future pull does not restore the local backlog.
- Keep unrelated research notes and `toolsweep-*` artifacts unless cleanup is
  explicitly requested. Link GitHub issues from any retained research notes.

## When a skill says "publish to the issue tracker"

Create or update the GitHub issue. For a multiline body, write it to a temporary
file and use `gh issue create --repo jstamagal/js --title ... --body-file ...`
with the applicable labels. Record the resulting URL. A local Markdown file
is only a draft until this succeeds.

## When a skill says "fetch the relevant ticket"

Use `gh issue view <number> --repo jstamagal/js --comments`. If the reference
is a legacy `.scratch/` path, read it and check GitHub for an existing issue.
When asked to triage that backlog, publish confirmed, still-open bugs that
have no existing issue; record fixed, duplicate, or unverified reports in the
triage notes.

## Wayfinding operations

Used by `/wayfinder` for local research. The **map** is a file with one
**child** file per research task. These files can remain local; actionable
project issues discovered through that research belong on GitHub.

- **Map**: `.scratch/<effort>/map.md` — the Notes / Decisions-so-far / Fog body.
- **Child ticket**: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the ticket type (`research`/`prototype`/`grilling`/`task`); a `Status:` line records `claimed`/`resolved`.
- **Blocking**: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every file it lists is `resolved`.
- **Frontier**: scan `.scratch/<effort>/issues/` for files that are open, unblocked, and unclaimed; first by number wins.
- **Claim**: set `Status: claimed` and save before any work.
- **Resolve**: append the answer under an `## Answer` heading, set `Status: resolved`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.
