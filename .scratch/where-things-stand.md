# Where things stand — 2026-08-11

Durable state from the 2026-08-11 session. Conclusions, not conversation.

## Merged
- Context compaction was deepened into one module (`js/compaction.py`) owning
  the decision and the deed. Merged to `main` as 03d05e1. The three triggers
  (REPL between-turn, mid-turn budget, overflow recovery) funnel through it.

## Unmerged branches — real work still sitting in worktrees
None of these are in `main`. All have committed work. Verified 2026-08-11.

- `pipe/jsdriver/headless-cli-integration` — Restore the working directory in
  new `-C` session tests
- `pipe/jsdriver/login-model-curation` — Make cached model editing offline and
  directly callable
- `pipe/jsdriver/runtime-size-recovery` — Recover size failures after tools run
- `pipe/jsdriver/session-foundations` — Treat exited session processes as no
  longer live
- `beefup/responses-first` — Use each turn's active tool context
- `fix/agent-driver-warts` — Error taxonomy and checkpoint work

`wiki-out` is NOT intentional design — verified 2026-08-13: it is one stale
commit reverting an enum fix, in service of a "wiki rewrite on another branch"
that exists nowhere. The real wiki gut IS merged (bc893dd). Safe to delete.
(An earlier version of this note said "intentional, ignore it" — that claim
was wrong and taught agents not to look.)

The jsdriver lanes are a ~16h stretch of work from before. The code may not
line up with `main` anymore — the repo has been heavily edited since. They
need a review per branch, not a blind merge.

## Prompt
`prompts/defaultagent/01-prompt.md` was rewritten by hand (owner's own edit,
not an agent's). Notable additions: CODE APE restructured into four numbered
rules; SYSOP escalation ladder; a KING-wrong callout block; a self-caught lie
block; the GATE/HOLD/STOP escalation. The lie framing now reads: caught wrong
is not a lie, hidden wrong is.

## Environment briefing
`prompts/defaultagent/02-env.md` is the "where the hell am I" block — machine
state injected at load by the harness, with a provenance marker so agents use
it without treating it as a message from the owner. It replaced a confusing
SYSOP instruction. Design rules: static truth in, dynamic truth earned via
tool calls, network state out, drivers out.

## AGENTS.md
Rewritten plain (no persona voice). Now short. Owner keeps editing it.

## Architecture review — next candidate
An architecture review surfaced seven deepening candidates (report:
`/tmp/architecture-review-20260811-155752.html`). Compaction was #1 and is
done. Next candidate is #2: a `Limits` value object in `js/config.py`,
derived once from Config, with `Limits.inherit()` for subagents. Current
`ToolContext` has 37 fields and three hand-copied tables; `_child_context`
already drifted (subagents lose `vision_enabled`, `jsonl_max_line_chars`,
`subagent_max_workers`). This is a real bug, not theoretical. Not urgent.
