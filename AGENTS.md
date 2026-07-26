# AGENTS.md

Guidance for coding agents working in this repository. `CLAUDE.md` is a symlink
to this file — one set of instruction, every agent read the same thing.

🦍 read this first. then 🦍 work.

## what `js` is

`js` = personal terminal LLM harness, Python, one silverback (the owner), one box.
no customer, no prod, no other dev. interactive chat, one-shot `-p`, pipe
workflows, local hacking tools, parallel subagents, wiki/artifact/drain modes,
commit-agent — all over the Vercel AI Python SDK (`ai`). bias = low friction,
many knob. when owner say remove, it GONE — deleted, no rename, no compat alias,
no hide.

## WORKFLOW — read this, the order matter

1. **`just` is the one entry point.** uv own the venv, so every command go
   through `just`. `just` with no arg list every recipe.
2. **env feel broken? → `just sync`.** that rebuild the env from `uv.lock`. it
   the real fix.
3. **make the change.**
4. **run the test that cover it** (see below). 🦍 run it, 🦍 read the output,
   then 🦍 say done. green before done.
5. **`just lint`** before 🦍 call it clean. ruff is the gate.
6. **commit regularly with plain git and a plain-English message.** commit is
   LOCAL and trivially undone (`git reset`, `git revert`, `--amend`), so it need
   no permission — commit free, quiet, often. 🦍 keep the git mechanic silent
   (hash, staging, "let me commit") — that noise the owner hate. a real
   defect/risk/decision 🦍 DO bark.
   - **commit subject = PLAIN ENGLISH.** `Restore removed files through
     symlinked paths` — a plain sentence, like a plain person wrote it. robot
     git-log-speak (`fix(scope):`, `feat(x):`, `docs:`) mean a MODEL leaked
     convention over the owner voice; 🦍 write the plain sentence instead.
7. **backup in place of push.** owner server is down, so after a batch of work
   copy the tree to a different physical drive: `cp -a js /opt/tempbackup/js<N>`,
   incrementing N. how it got done in the 90s. easy peasy lemon squeezy.

burn it in: **commit = reversible, do it.**

## commands

```bash
just                 # list every recipe
just sync            # rebuild env from uv.lock (extra: test). the fix for a broken venv.
just run -p "..."    # run js (REPL with no args). forwards all flags
just test            # offline suite: -m "not ai_provider and not vision" -p no:cacheprovider
just test-file tests/test_picker.py        # one file or node
just test-mark "not ai_provider"           # by pytest marker
just lint            # ruff check . (errors + pyflakes + pyupgrade) — the quality gate
just fix             # ruff safe auto-fixes only; leaves unused imports and formatting alone
just build           # uv build -> sdist + wheel
```

run one test directly when 🦍 need a single node:
`uv run --extra test pytest -q tests/test_foo.py::test_bar`

**live tests** (`ai_provider`, `e2e`, `vision`) need real provider creds or a
local OpenAI-compatible / vision endpoint — `just test` skips them on purpose.
`just test-live` run them. markers defined in `pyproject.toml`.

**ruff exclude:** `js/toolkit/wiki/prompts.py` (giant prompt-template builder,
linting it pure noise). **mypy was tried and DROPPED** — ~115 unactionable
errors on a dynamic codebase (ToolContext dynamic attrs, `**kwargs` splat,
implicit optionals). this repo run with no type gate; re-add it when owner say.

## architecture — the big picture

streaming tool-use loop, sync runtime, async SDK underneath. the parts that
need reading many files to see:

- **`js/model_client.py` — the ONE provider boundary.** the only production
  module that import `ai`. it adapt the SDK async/part-based API down to the
  sync/dict-based runtime. all model I/O cross here, and `import ai` live here
  alone — new provider plumbing land in this file.
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
  always on; code subs (`sh`,`bash`,`python`,`node`,`c`) ON by default
  (`runtime.allow_inline_code`), opt out per-run with `--im-a-pussy` or
  `JS_ALLOW_INLINE_CODE=0`. SINGLE pass = the injection guard: output stay
  unscanned. backtick-wrap or a leading `\` keep a directive LITERAL (for docs).
  🦍 preserve both property if 🦍 touch this.
- **`js/config.py` + `js/settings.py` — config layering.** jsrc (global)
  < `.js/jsrc` < `.js/jsrc.local` < env (`JS_MODEL`, `JS_PROVIDER`, `JS_BASE_URL`,
  `JS_API_KEY`, etc.) < `--extra` CLI flag. Config files are scripts: each line is
  a `set <key> <value>` command (no TOML; legacy `config.toml` migrated via
  `js --migrate-config`). Set model id via `set model.id <value>`. Official SDK env
  vars (`AI_GATEWAY_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`)
  are read by `ai` directly when no explicit provider set.
- **sessions = append-only JSONL** at platform-data `sessions/<agent_id>/<session>.jsonl`,
  one isolated state per agent. **compaction APPENDS marks and leave history
  intact** (`/compact [focus]`, `/compact up to here`, `js --compact <session>`).
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

## standing facts — 🦍 keep these true

- **canonical tool names only.** the live names are in
  `docs/tools-reference.md`. old names (`fs_read`, `fs_write`, `cat`, `grep`,
  `semantic_search`) are dead and stay dead.
- **session history store canonical lowercase tool names.** any
  provider-facing name mangling stay inside `model_client.py` and reach the
  wire only — what land in the JSONL is the canonical form.
- **owner deliberate edits are relics** — rage-text, quips, hand-tuned comment
  alignment. leave them exactly as they sit. `just format` collapse intentional
  alignment across ~110 files — run it when owner say, and review the diff.

## docs

deep dives live in `docs/` — `technical-guide.md` (internals),
`tool-system.md`, `subagents.md`, `inline-directives.md`,
`configuration-and-sessions.md`. `CHANGELOG.md` track what moved.
