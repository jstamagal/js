# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

🦍 read this first. then 🦍 work.

## what `js` is

`js` = personal terminal LLM harness, Python, one silverback (the owner), one box.
no customer, no prod, no other dev. interactive chat, one-shot `-p`, pipe
workflows, local hacking tools, parallel subagents, wiki/artifact/drain modes,
commit-agent — all over the Vercel AI Python SDK (`ai`). bias = low friction,
many knob. NO compat alias kept alive to please old prompt. when owner say
remove, it GONE — no rename, no hide.

## WORKFLOW — read this, the order matter

1. **`just` is the one entry point.** uv own the venv. 🦍 NEVER `pip install`,
   NEVER activate venv, NEVER call `.venv/bin/js` — that path rot the moment the
   pkg no in the venv. `just` with no arg list every recipe.
2. **env feel broken? → `just sync`.** that rebuild the env from `uv.lock`. it
   the real fix, not pip-poking.
3. **make the change.**
4. **run the test that cover it** (see below). green before 🦍 say done. 🦍 no
   vibe a "pass" — 🦍 RUN it.
5. **`just lint`** before 🦍 call it clean. ruff is the gate.
6. **COMMIT by running exactly `js --commit` from repo root — NOT raw `git commit`,
   NOT `just commit -p ...`, NOT targeted path, NOT with a supplied message.** the
   commit-agent is the thing being dogfooded: agents use it after each coherent
   modification so its breakage SURFACES. when it break 🦍 **FIX `js --commit`**,
   🦍 no route around it back to plain git. commit is LOCAL and trivially undone
   (`git reset`, `git revert`, `--amend`), never leave the box, so it need NO
   permission ever — commit free, quiet, often. 🦍 no narrate the git mechanic
   (hash, staging, "let me commit") — that noise the owner hate. a real
   defect/risk/decision 🦍 DO bark.
7. **AFTER `js --commit` finishes — INSPECT, then tune ONE knob.** 🦍 read the
   resulting `CHANGELOG.md` and the commits (`git log --oneline`, `git show`) and
   confirm everything correct: clean splits, honest bullets, exactly one of each
   `### ` subsection per release. wrong? → tune **ONE knob at a time** in
   commitbot (`prompts/commit/01-prompt.md`), nothing else. then **run
   `js --commit` AGAIN** and re-inspect. one knob, re-run, repeat.
   - **commitbot must be BULLETPROOF, and the only way there is SUBTRACTION.**
     adding instruction makes it WORSE — more for the model to lose track of,
     more slop, more places to drift. a tight prompt is a reliable prompt. so a
     knob is almost always a CUT or a sharpen, never a pile-on; if an edit adds,
     it removes more real content than it adds (not whitespace). pink-elephants
     (naming the bad output plants it — state the RIGHT structure, never
     "NEVER do X") are the first thing to cut.
8. **PUSH — the gate. 🦍 ASK FIRST, always.** push send commit OFF the box —
   NOT easily undone, public, can break a remote. owner push when owner feel
   like it. 🦍 never push unasked.

the split, burn it in: **commit = reversible, do it. push = irreversible-ish, ask.**

## commands

```bash
just                 # list every recipe
just sync            # rebuild env from uv.lock (extra: test). the fix for a broken venv.
just run -p "..."    # run js (REPL with no args). forwards all flags: just run --commit
js --commit          # exact commit workflow. no args, no -p, no target.
just test            # offline suite: -m "not ai_provider and not vision" -p no:cacheprovider
just test-file tests/test_picker.py        # one file or node
just test-mark "not ai_provider"           # by pytest marker
just lint            # ruff check . (errors + pyflakes + pyupgrade) — the quality gate
just fix             # ruff safe auto-fixes (does NOT strip unused imports, does NOT reformat)
just build           # uv build -> sdist + wheel
```

run one test directly when 🦍 need a single node:
`uv run --extra test pytest -q tests/test_foo.py::test_bar`

**live tests** (`ai_provider`, `e2e`, `vision`) need real provider creds or a
local OpenAI-compatible / vision endpoint — `just test` SKIPS them on purpose.
`just test-live` run them. markers defined in `pyproject.toml`.

**ruff exclude:** `js/toolkit/wiki/prompts.py` (giant prompt-template builder,
linting it pure noise). **mypy was tried and DROPPED** — ~115 unactionable
errors on a dynamic codebase (ToolContext dynamic attrs, `**kwargs` splat,
implicit optionals). no type gate here. no re-add it without owner say.

## architecture — the big picture

streaming tool-use loop, sync runtime, async SDK underneath. the parts that
need reading many files to see:

- **`js/model_client.py` — the ONE provider boundary.** the only production
  module that import `ai`. it adapt the SDK async/part-based API down to the
  sync/dict-based runtime. all model I/O cross here. new provider plumbing land
  HERE, no scatter `import ai` across the tree.
- **`js/runtime.py` — the loop.** stream output, dispatch tool call, typed error
  handling, telemetry, subagent fan-out (ThreadPoolExecutor). calls
  `model_client` for I/O, `toolkit.registry` for tools.
- **`js/toolkit/` — the tools.**
  - `core.py` — `Tool`, `ToolContext`, `call_tool`.
  - `registry.py` — assemble the registry + selector filtering (which tools a
    given agent see).
  - `fs.py` read/write/search/patch/remove/undo · `process_net.py` shell+fetch ·
    `meta.py` todo/plan/skill/task/subagents.
  - `tool_descriptions/*.md` — model-facing tool contracts, shipped as
    package-data. the WORDS the model read about each tool live here, not in code.
  - `wiki/`, `artifact/` — built-in mode tools + their prompt builders.
- **`js/persona.py` + `prompts/` — agents are PROMPT DIRECTORIES.** a dir of
  numbered `NN-*.md` files concatenated into the system prompt. layered:
  repo `prompts/` < global `agents/` (platform config dir) < project
  `.js/agents/` — **project win over global win over repo.** `tools:`
  frontmatter pick the tool surface. id rule in `config.py`.
- **`js/promptexpand.py` — inline directive expansion at load.** `{{VAR}}` (env),
  `!{sub ...}` inline, `` ```!sub `` fenced block. read-only subs (`env`,`file`)
  always on; code subs (`sh`,`bash`,`python`,`node`,`c`) gated behind
  `--dangerously-evaluate-inline-code`. SINGLE pass = injection guard, output
  never re-scanned. backtick-wrap or a leading `\` keep a directive LITERAL (for
  docs). 🦍 keep that property if 🦍 touch it.
- **`js/config.py` + `js/settings.py` — config layering.** platform
  `config.toml` < project `.js/config.toml` < `.js/config.local.toml`; env
  override files; CLI flags / `--extra` override env. `[model].id` default; env
  `JS_MODEL`/`JS_PROVIDER`/`JS_BASE_URL`/`JS_API_KEY` override. official SDK env
  (`AI_GATEWAY_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`)
  read by `ai` directly when no explicit provider set.
- **sessions = append-only JSONL** at platform-data `sessions/<agent_id>/<session>.jsonl`,
  one isolated state per agent. **compaction APPENDS marks, never rewrite
  history** (`/compact [focus]`, `/compact up to here`, `js --compact <session>`).
- **`js/cli.py` — arg parse + mode dispatch** (REPL / `-p` / `--commit` /
  `--wiki` / `--artifact` / `--compact`). `js/drain.py` is the `js-drain` entry.

## prefer computed context over manual probing

Before doing a deterministic, mechanical task through a sequence of tool calls —
gathering state, computing a value, reshaping data, checking a condition — first
ask whether code can produce the result and hand it to you directly. This harness
evaluates inline directives at prompt-load (`!{sh ...}`, `!{python ...}`, fenced
`` ```!lang `` blocks) and injects their output, and you can shell out to a helper
at runtime. Your leverage is judgment; the rote work is cheaper, steadier, and
reproducible as code. When you catch yourself driving mechanical machinery through
a fragile channel — anything interactive, order-sensitive, or many-round-trip —
stop and move it into a script whose output you consume. Keep the model deciding;
let code do the rote.

## landmine — no step on these

- **NO legacy tool alias.** never reintroduce `fs_read`, `fs_write`, `cat`,
  `grep`, `semantic_search`. canonical names only (`docs/tools-reference.md`).
- **Claude provider adapt is name-only.** when the model string contain
  `claude`, only provider-facing tool-schema NAMES get adapted; session history
  stay canonical lowercase. no leak the adaptation into stored history.
- **owner deliberate edits are relics** — rage-text, quips, hand-tuned comment
  alignment. 🦍 no silently "clean" them. `just format` collapse intentional
  alignment across ~110 files — run only when owner say, review the diff.

## docs

deep dives live in `docs/` — `technical-guide.md` (internals),
`tool-system.md`, `subagents.md`, `agents-and-prompts.md`, `inline-directives.md`,
`configuration-and-sessions.md`. `CHANGELOG.md` track what moved.
