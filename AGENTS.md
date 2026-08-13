# AGENTS.md

Guidance for coding agents working in this repository. `CLAUDE.md` is a symlink
to this file — one set of instructions, every agent reads the same thing.

## What `js` is

A personal terminal LLM harness in Python: one owner, one machine, no
customers, no production, no other developers. Interactive chat, one-shot
`-p`, pipe workflows, parallel subagents, wiki agents, commit-agent — built on
the Vercel AI Python SDK (`ai`). Bias: low friction, many knobs. When the
owner says remove, it is gone — deleted, not renamed, no compatibility alias.

## Workflow

1. **`just` is the entry point.** `just` alone lists every recipe. uv owns the
   venv — `uv.lock` is the truth, so package changes go through uv; a
   `pip install` into `.venv` gets erased by the next `just sync`. Run js
   through `just run`, not `.venv/bin/js` (which goes stale).
2. **Env feels broken? → `just sync`.** It is the real fix.
3. **Make the change.**
4. **Run the tests that cover it.** Green before done.
5. **`just lint`** before calling it clean. ruff is the gate.
6. Commit regularly. It's local. It's trivially undone (`git revert`, `--amend`), so it needs no permission — commit free, quiet, often.

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
(`js/persona.py` + `prompts/`; layered project > global > repo; `tools:`
frontmatter picks the tool surface). Config layers jsrc < `.js/jsrc` <
`.js/jsrc.local` < env < `--extra`. Sessions are append-only JSONL and
compaction leaves history intact. Inline-directive expansion
(`js/promptexpand.py`) is single-pass on purpose — that is the injection
guard; preserve it. Deep dives in `docs/technical-guide.md`.

## Docs

- Deep dives live in `docs/` (). `
- Agent-skill docs: `docs/agents/` (issue tracker — issues as
markdown under `.scratch/<feature>/`; triage labels; single-context domain docs with `CONTEXT.md` + `docs/adr/`).
