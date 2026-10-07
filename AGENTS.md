# AGENTS.md

Guidance for coding agents working in this repository. `CLAUDE.md` is a symlink
to this file — one set of instructions, every agent reads the same thing.

## What `js` is

A personal terminal LLM harness in Python: one owner, one machine, no
customers, no production, no other developers. Interactive chat, one-shot
`-p`, pipe workflows, parallel subagents, wiki agents, commit-agent — built on
the Vercel AI Python SDK (`ai`). Bias: low friction, many settings. When the
owner says remove, it is gone — deleted, not renamed, no compatibility alias.

## Workflow

1. **`just` is the entry point.** `just` alone lists every recipe. uv owns the
   venv — `uv.lock` is the truth, so package changes go through uv; a
   `pip install` into `.venv` gets erased by the next `just sync`. Run js
   through `just run`, not `.venv/bin/js` (which goes stale).
   Dependencies are not hard-pinned with `==`. Keeping them current is a
   recurring chore: bump them, relock, run the suites, bump the version. An
   upper bound needs the incompatibility it guards against named beside it.
2. **Env feels broken? → `just sync`.** It is the real fix.
3. **Make the change in a worktree.** The `~/js` checkout stays on main: the
   `js` on PATH runs whatever it has checked out, so a branch left there
   changes the js the owner uses. Work on a branch in its own worktree:
   `git worktree add ../js-<topic> -b <topic>`.
4. **One bug, one test, one seam.** Write one test that fails on the bug.
   Fix the bug. Run that test. Then run the test file for the seam you
   touched (`just test-file tests/test_<seam>.py`). That is the whole loop.
   A second test needs a second distinct behavior to pin. Wording, comments,
   docstrings and messages get no test. Do not run `just test` after an
   edit: it runs 2400+ tests on every logical core and lags the box. Run it
   once before a merge. It is cached per tree state — an unchanged tree
   replays the last run and says who ran it, so rerunning tells you nothing
   new. Whose change broke a test does not matter; "it was failing before
   me" is not a state of the tree, it is a description of you walking past
   it. Do not delete the test. Do not edit the test to pass. If you cannot
   make it pass, the only other way out is a written confession: append to
   `FAILURES.md` at the repo root your full model name, the date, the failing
   test names, what you tried, and the sentence "I could not make this pass."
   A confession is public and permanent; that is the point.
5. **`just lint`** before calling it clean. ruff is the gate.
6. Commit regularly. It's local. It's trivially undone (`git revert`, `--amend`), so it needs no permission — commit free, quiet, often.
   Commits are authored by whoever made them. The owner is
   `John Stamagal <jstamagal@gmail.com>` and that is the global config. A model
   commits as itself, name and vendor, set per command with
   `-c user.name=... -c user.email=...`: `Claude Fable 5.1 <claude@anthropic>`,
   `Qwen <qwen@alibaba>`. The commit agent is
   `js commit agent <jstamagal+agent@gmail.com>`.
7. **Merge it.** A branch nobody merged is organized forgetting — the work is
   not done while it sits unmerged. Merge to main when green; if main moved
   underneath, review then merge. Never park work on a branch silently: 28
   commits rotted that way once. Saying "awaiting review" once and moving on
   counts as silent. Once merged, remove the worktree and delete the branch.
8. **Say where the work is.** The owner does not do git. End every report
   with one plain line: the branch the work is on, whether main has it,
   whether it is pushed. Anything he has to run, give as the exact command.

## Privacy 
- Keep our chat out of the files unless its relavent.
Commentary about having a hard drive go bad - No.
Commentary about technical discussions in which we arrived at a genuine shared conclusion and information is not a temporary state - Yes.

## Commands
`just` lists everything. The daily few: `just sync` (rebuild env — the fix
for a broken venv), `just run -p "..."` (run js; REPL with no args),
`just test` (offline suite), `just test-file <path>`, `just lint`,
`just check`. Focused suites exist (`just test-tools`, `test-wiki`,
`test-runtime`, …). Live tests (`ai_provider`, `e2e`, `vision`) need real
provider creds: `just test-live`. One test directly:
`uv run --extra test --extra browser pytest -q tests/test_foo.py::test_bar`

## Architecture in one breath

Streaming tool-use loop: sync runtime over the async SDK.
`js/model_client.py` is the model I/O boundary (new providers land next to
`codex_provider.py`); `js/runtime.py` is the loop (streaming, dispatch,
subagent fan-out); `js/toolkit/` is the tools — model-facing contracts live
in `tool_descriptions/*.md`, not in code. Agents are prompt directories
(`js/persona.py` + `prompts/`; layered project > global > repo; `agent.yaml`
beside the prompt picks model, reasoning and tool surface, resolved against
`tools.yaml` noun:modifier rules). Config layers jsrc < `.js/jsrc` <
`.js/jsrc.local` < env < `--extra`. Sessions are append-only JSONL and
compaction leaves history intact. Inline-directive expansion
(`js/promptexpand.py`) is single-pass on purpose — that is the injection
guard; preserve it. Project instructions come from the start directory only
(`AGENTS.md`, `AGENTS.local.md`): js never walks up to parent directories, so
`~/js` never reads `~/AGENTS.md`. Deep dives belong in `docs/technical-guide.md`.

Anything settable is a registered setting (`js/settings.py`). `/set` with no
argument dumps every settable setting, `/set <key> <value>` sets one, and
`/save` rewrites jsrc from the full live state, no confirmation. A setting
reachable only through an env var or a hardcoded default is not finished.
A setting a turn reads is also one `TurnSetting` row in `js/turn_settings.py`;
the row makes it a `Config` field and, with `on_context`, a `ToolContext` field
that subagents inherit.

## Docs

Deep dives belong in `docs/`: reference material, benchmark results, design
write-ups. If you produce that kind of thing, it goes there.

Trust code over docs. Docs go stale; the code is what runs every day, and
nobody here rereads `docs/` to keep it current. When a page in `docs/`
disagrees with the code, the code is right. If you are changing the area a
stale page describes, correct or delete the stale text in the same change.

Issues are beads: `bd ready` for open work, `bd show <id>` for the spec
pointer and acceptance, `bd create` to file, `bd close <id>` when done,
`bd sync` after a batch. Never `bd delete` a child id (`js-x.N`) — it can
take the parent epic with it. Design docs live in `~/.js/work/`.
`.scratch/` is for a reproduction you are still building, not a backlog.
Leave `toolsweep-*` artifacts alone unless explicitly asked to clean them up.

## Tests and comments

Tests pin what must be true for the code to be correct. Do not assert on
wording, presentation, or styling that could change without anything being
wrong.

Comments state what is true about the code. Do not turn something someone
said once into a rule in the code, and do not describe what the code does not
do or alternatives nobody asked about. Describe what is there.
