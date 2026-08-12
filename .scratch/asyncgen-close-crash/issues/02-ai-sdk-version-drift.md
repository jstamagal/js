# The repo pins ai 0.2.1 while the installed `js` runs ai 0.4.2

Category: bug
Status: needs-triage

Reported: 2026-08-12 (found while triaging `01-httpcore2-asyncgen-error-after-turn-ends`)

## What's wrong

The `js` on `PATH` — the uv tool install, the one actually used day to day —
resolves a different dependency stack from the one `uv.lock` pins:

| | `uv.lock` (repo, `just run`) | uv tool install (`js` on PATH) |
| --- | --- | --- |
| `ai` | 0.2.1 | 0.4.2 |
| http transport | `httpx 0.28.1` / `httpcore 1.0.9` | `httpx2 2.10.0` / `httpcore2 2.10.0` |

`httpcore2` is not installed in the repo env at all.

## Why it matters

`just test` and `just run` exercise a stack that is two minor versions of the
SDK behind what every real run uses. Issue 01 is a concrete instance: a
deterministic, every-run defect that is **invisible** to the repo checkout and
to the test suite, and only appears in the install being used. Any bug living
in the newer transport is currently untestable here.

`ai` is the dependency the whole harness sits on — `model_client` adapts its
async, part-based API down to the sync, dict-based runtime — so a 0.2 → 0.4
jump is not a routine bump and wants its own verification pass, not a
drive-by.

## Open questions

- Was the tool install deliberately kept ahead of the lockfile, or did it
  drift by being installed at a later date?
- Does `ai` 0.4.2 change the streaming event/part surface that
  `_stream_async` and the usage/metadata extraction read?
- Should the tool install be pinned to the lockfile instead (`uv tool install`
  from the repo), so there is exactly one stack?

## Not yet established

- Whether `just test` passes against 0.4.2.
- What else in the 0.2 → 0.4 range is breaking.
