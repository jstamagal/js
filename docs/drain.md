# Drain

`js-drain` drains a wiki inbox or arbitrary folder into a wiki by running
sequential `js --wiki=ingest` jobs sized to fit the model.

It is intentionally sequential because ingest mutates shared vault state:
pages, logs, overview/index files, and optional git state.

## Commands

```bash
js-drain creative
js-drain creative -a
js-drain creative -f ~/dump
js-drain creative -f ~/dump -a
js-drain general -b 6000
js-drain creative -R
js-drain creative -n
js-drain creative -l 3
js-drain ~/vaults/notes -M ingest,synthesize -t 600 -a
```

## Defaults

By default, drain leaves originals exactly where they are. It ingests and does
not move files.

With `-a` / `--archive`, drain moves drained originals to:

```text
<vault>/Clippings/
```

Only after every job covering that original succeeds.

## Planning

The unit of work is a source-token budget, not a file.

Small files can be packed together into bundle jobs. Large text files are split
line-aligned into multiple pieces. Large binary/undecodable files are passed
whole.

Budget:

- `-b`/`--budget` overrides; otherwise `auto_budget_tokens(model)` asks models.dev
  for the active model's context window and budgets a conservative fraction of it.
  If the model is unknown, it falls back to a small fixed budget.
- The model resolved is the same one `js` would use: layered config, then
  `JS_MODEL`, with `--model` overriding all.

The rough conversion is:
```text
budget_chars = budget_tokens * 4
```

## Ralph Mode

`-R` / `--ralph-wiggum` disables packing and splitting. Every file becomes one
whole-file job and JS/wiki is allowed to archive top-level inbox files.

## Limit

`-l N` runs only the first `N` jobs. This is for smoke tests.

Important archive behavior:

- drain still builds the full job plan first
- execution is sliced to the limit
- archive eligibility is based on the full plan

That prevents a split original from being archived just because the first
limited slice succeeded while later slices were never run.

## Archive Ownership

Some jobs can be archived by `wiki_finish_ingest`; others must be moved by
drain.

Drain owns archive movement for:

- bundles
- split pieces
- nested inbox files
- arbitrary source folders passed with `--from`

Top-level inbox files in non-bundled jobs can be archived by wiki.

When not archiving, drain sets `JS_WIKI_NO_ARCHIVE=1` for subprocesses so the
wiki tools also leave originals in place.

## Execution

Each job runs:

```bash
python -m js.cli --wiki <modes> --vault <vault> -n <job.feed>
```

The `-n` means the subprocess does not save a normal agent session. Wiki state
is persisted to the vault files themselves.

`--model` passes a model override to every job. Without it, child jobs inherit
the configured/env model. `--timeout` kills hung jobs.

## TUI And Summary

On a TTY, drain repaints an in-place progress display. When output is piped, it
prints line-per-event progress.

At the end it prints:

- done count
- failed count
- interrupted count
- retry command for each failed job

Failed or interrupted drains return exit code `1`; clean drains return `0`.

## Final Commit

If the vault is a git repo and this is not a dry run, drain stages and commits
at the end with:

```text
drain: <modes>
```

The commit is skipped when there is nothing staged.
