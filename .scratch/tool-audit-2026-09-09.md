# Tool system audit + bench session — 2026-09-09

Everything from a long session that started as "compare two PLAN.md files" and ended
up finding bugs in the toolkit, the tool bench, and the audits themselves.

Evidence standard used throughout: **[VERIFIED]** = reproduced by running code in this
session. **[SOURCE]** = read from source, logic checked, not executed.
**[REPORTED]** = an audit agent claimed it, not independently confirmed here.
Where I got something wrong earlier in the session it is marked **[I WAS WRONG]**,
because the wrong version may have leaked into notes elsewhere.

---

## 1. Fixed and committed

### 1.1 `patch` schema broke grammar-constrained models — `63f7610` [VERIFIED]

`patch` declared `input_schema` as a bare `{"type":"object","oneOf":[...]}` with **no
top-level `properties`**. llama.cpp's schema-to-grammar path walks `properties`, finds
none, and emits `{}`.

Measured against local qwen3.8-27b via ollama: three consecutive `patch` calls with
literally `{}` as arguments, retry limit hit, zero changes, task dead. After flattening,
the same task landed two multi-edit patches across two files and passed 53 tests.

The repo had already learned this lesson one file over — `meta.py:630`:

```python
# No regex pattern here: llama.cpp's schema-to-grammar path
# chokes on them, and todo_write strips/validates in code.
```

Fix: one flat object, five properties, `required: ["file_path"]`. The `oneOf` was
enforcing "scalar form or batch form, never both", which `_apply_edit` already enforces
in code (`fs.py:598-602`), so nothing was lost. Error message also improved — now
`'file_path' is a required property` instead of `not valid under any of the given schemas`.
Schema 1165 → 1041 bytes.

**Why it mattered:** ollama is grammar-constrained, vLLM is not. Left unfixed, the 27B
would have scored ~0 on every edit task and flash-next wouldn't, and it would have read
as a model-quality gap.

Test `test_patch_schema_has_complete_scalar_and_nonempty_batch_forms` asserted
`schema["oneOf"]` directly; rewritten to assert the same guarantees against the flat
shape plus `"oneOf" not in schema` with the reason. 107 tests pass.

### 1.2 Bench: reasoning effort never reached any model — `f4ce13e` [VERIFIED]

`model_env()` in `bench/toolbench/run.py` set `JS_MODEL_REASONING_EFFORT`. **Nothing in
`js` reads that name.** The real setting is `model.reasoning_effort`, env `JS_REASONING`
(`settings.py:137`). So `suite.toml`'s `reasoning_effort = "high"` has never reached a
model in the history of this bench.

Renaming alone was not enough: `sandbox.sh:47` forwards a fixed list of vars into the
container and still listed the old name. Both fixed. Added `TOOLBENCH_REASONING_EFFORT`
override matching the other `TOOLBENCH_*` knobs, since one value can't suit every model
(deepseek serves `max`, qwen wants `xhigh`, and `high` is not valid on qwen).

Verified `JS_REASONING = xhigh` lands in the container env.

### 1.3 Bench: public HTTPS endpoints unreachable — `f4ce13e` [VERIFIED]

`resolve_base_url()` swapped every non-literal hostname for its A record so containers
could reach Tailscale/hosts names. That rewrote `https://api.deepseek.com` to
`https://3.173.21.63`, where TLS fails on the bare CloudFront IP. Both deepseek arms died
`0/4` in seconds with `provider 'openai' connection failed`.

Now only private/LAN addresses get the swap. `yoda:42069` still resolves to
`10.0.0.197:42069`; `api.deepseek.com` keeps its hostname.

---

## 2. Fixed, uncommitted

### 2.1 Bench: parallel arms raced on shared state [VERIFIED]

Two concurrent `run.py` invocations share `/tmp/js-toolbench/repos/<repo>/.reporacer/config.json`
and one `results.jsonl`. Each arm rewrote the config with **only its own agent**.
Observed: `click/config.json` ended up listing only `js-full-stock`, so all four of the
slim arm's click tasks died instantly with `Agent command is not configured: js-full-slim`,
and stock's rows were being lost from the shared results file.

`--work` already exists as a runner flag and was never used. Fix is one flag per arm:
`--work /tmp/js-toolbench-<label>`. Verified isolated: each arm's `config.json` then
contained exactly its own agent.

This lives in a matrix driver script that did not survive the session. If parallel arms
are ever run again, `--work` per arm is mandatory.

### 2.2 `lazy-everything` experiment — uncommitted in `js/toolkit/registry.py`

`_LAZY_SUITES` currently maps **every** native tool to a suite, so the boot surface is
`tool_discovery` alone.

```
boot surface: ['tool_discovery']
boot bytes  : 1,191        (was ~28,000 for the 27-tool eager surface)
reachable   : 27 tools via discover
```

Revert with `git checkout -- js/toolkit/registry.py`.

**Do not run `just toolbench-image` while this is applied** unless you intend the bench
to measure the deferred config — the image bakes a wheel from the tree.

Three `test_lazy_tool_discovery.py` tests fail with this applied, all asserting `read` is
eager (e.g. `assert _names(surface) == ["read", "tool_discovery"]`). Those failures are
the point of the experiment, not cleanup — which way to fix them is a design decision.

---

## 3. Known-broken, not fixed

### 3.1 `hide_files = true` does nothing [VERIFIED]

`suite.toml:11` sets it, `run.py:374` turns it into `TOOLBENCH_HIDE_FILES=1`,
`sandbox.sh:47` forwards it into the container — and **nothing reads it**. No consumer
anywhere in the tree. The prompt is written by `reporacer`, which doesn't know the
variable exists.

Consequence: every task prompt lists the files the historical commit touched, **including
the test file the grader owns**:

```
Files touched by the historical commit:
- src/click/shell_completion.py
- tests/test_shell_completion.py
```

Combined with 3.2 below, this seeds the model with a big red button and then punishes it
for pressing it.

### 3.2 The miner mines unwinnable tasks [VERIFIED]

The bench withholds the historical commit's test change, lets the agent work, then applies
`hidden-tests.patch` and runs the suite. That only works if the mined test change is an
**addition**.

click task-001 (`b7e5fd4 "Fix broken fish completion and multiline help string"`) changed
the test file `3 insertions(+), 50 deletions(-)` — the human **deleted**
`test_fish_multiline_help_complete` and rewrote three expectations.

So:
- Reproduce the commit → you delete the test → `git apply` conflicts → `internal_error`
- Don't reproduce it → the old test asserts the old buggy format → assertion fails

**There is no state that scores.** And the prompt says *"Do not delete or weaken tests"*
while asking the agent to recreate a commit whose entire test change is a deletion.

`suite.toml` filters on `min_changed_files`, `max_changed_lines`, `hidden_test_patterns` —
nothing rejects a net-negative test diff. That filter is the fix.

### 3.3 `internal_error` is a misleading status name [VERIFIED]

It covers at least three unrelated things: hidden-test patch conflict (which is usually
the harness's fault per 3.2), genuine harness failure, and agent-modified graded files.
A docker crash and a correct-but-unscoreable task produce the same word. In a results
table six months from now this will mislead.

### 3.4 TOOLSTATS doesn't survive into the results dir [VERIFIED]

`TOOLSTATS {...}` is emitted to agent stdout and captured in the per-task `.log` under the
**work dir** (`/tmp/js-toolbench*`), which is disposable. `run.py` copies `reporacer/` and
`telemetry/` into the stamped results dir but not those logs. When `/tmp` was cleared this
session, all tool-call telemetry for four completed runs was lost and had to be recomputed
from the session transcripts in `telemetry/`.

Either copy the task logs into the results dir, or write TOOLSTATS into `summary.json`.

### 3.5 `shell_habits.read_via_shell` is a bad metric [VERIFIED] [I WAS WRONG]

It pattern-matches `cat`/`sed`/`head`/`tail` anywhere in a shell command. Of 59 such
commands in the deepseek stock run:

```
41  piped into grep/awk/sed        ← read tool can't pipe
15  git show / git cat-file / git diff  ← read tool can't touch git objects
 3  plain cat/head of a tracked file    ← the only real substitution
```

I twice reported "the model prefers cat over your read tool" based on this counter. It
doesn't. It uses `read` for reading files and `shell` for things `read` structurally
cannot do. The metric should exclude `git show`-family commands and anything containing a
pipe.

### 3.6 The bench summary table shows `-` for tool calls [VERIFIED]

TOOLSTATS is captured but `summary.md` renders `-` in every tool-call column. The data
exists and isn't reaching the table.

---

## 4. Toolkit bugs found by the audit agents

Cited by content, not line number — several agents' line numbers drifted, and the tree has
moved since.

### Confirmed here

| # | Finding | Status |
|---|---|---|
| A | `patch` `oneOf` breaks grammar-constrained models | [VERIFIED], fixed `63f7610` |
| B | CRLF preservation machinery is dead code | [VERIFIED] |
| C | `read` schema rejects what the handler accepts | [VERIFIED] |
| D | `fs.py` `list_dir` is dead — zero references in `js/` or `tests/` | [VERIFIED] |
| E | `head_limit=0` silently returns the full default page | [VERIFIED] |
| F | `ast_search` `lang` enum is capitalized, handler casefolds | [VERIFIED] |
| G | `tooldiag` TOTAL sums three columns, docstring promises two | [VERIFIED] |

**B — CRLF.** `_write_bytes_preserving_existing_newlines` calls `path.read_text()`
(universal newlines) and then `_detect_line_ending(existing)`. `read_text()` has already
converted `\r\n` → `\n`, so the detector can only ever return `"\n"`. Verified
independently: `b"a\r\nb\r\n"` → `read_text()` → `'a\nb\n'` → detector returns `'\n'`.
Three helper functions and a documented guarantee in `write.md` ("Existing newline style
is preserved when overwriting"), dead by construction. `fs.py` even reads
`source_bytes = target.read_bytes()` on the line above and doesn't use it for detection.

**C — read schema.** The handler accepts `path`, `file_path`, `range`, `start_line`,
`end_line`. The wire schema declares only `file_path`, `range`, `show_line_numbers` with
`additionalProperties: False`, and `runtime.py` validates before dispatch. Meanwhile
`fs.py`'s own continuation footer emits
`[N total lines; read <path> with start_line=M to continue]`. Replayed the exact
validation:

```
REJECTED {"file_path":"long.txt","start_line":6,"end_line":10}
         -> Additional properties are not allowed ('end_line', 'start_line' were unexpected)
ACCEPTED {"file_path":"long.txt","range":{"start_line":6,"end_line":10}}
REJECTED {"path":"long.txt"}  -> 'file_path' is a required property
```

**The tool teaches the model a call the runtime rejects, on every page of every long
file.** Same shape for `write` and `patch` with `path`.

This showed up live in the benchmark: `ERROR: invalid arguments for read: [1, 180] is not
of type 'object'` — the model reaching for a positional range.

**G — tooldiag.** `tooldiag.py:7` says "the total of the two model-facing columns";
`:47` and `:53` sum `raw + rendered + schema`. `raw` is pre-render file size that never
ships. Any TOTAL read off that table is inflated.

### Reported by agents, not confirmed here

- Undo can delete a file when snapshot capture fails — `core.py` sets `content = None`
  on `OSError`, `fs.py` treats `previous is None` as "restore deletion" and unlinks.
  Mechanically consistent with the source; the fault-injection result is the agent's.
- `pop_snapshot` discards memory and disk records *before* attempting restore, so a
  transient restore failure leaves no snapshot to retry.
- `fs_search` dedup cache serves stale results after external (non-tool) mutation.
  The agent verified the tool-mediated path works and identified the hole.
- Undo / remove-dir / kernel / toolbox don't call `invalidate_search_cache()`.
- `read` PDF path ignores both size caps (reported ~400 MB RSS on a 200 MB PDF).
- `skill.md` documents discovery roots and layouts that don't match `skills.py`.
- Subagent `_child_context` hand-copies 18 fields and omits `vision_enabled`,
  `subagent_max_workers`, `shell_env_allow`.
- `check_schema()` runs on every tool call against static schemas (~2.5–6.6 ms each).
- Locale-dependent `read_text`/`write_text` in `fs.py` and `meta.py` vs explicit UTF-8
  elsewhere — under `LC_ALL=C`, read succeeds and patch fails on accented bytes.
- `line.rstrip()` in search output strips significant trailing whitespace.
- `search.py` catches `urllib.error.HTTPError` while importing only `urllib.request`.

---

## 5. Tool exposure / discovery

### What exists and works [VERIFIED]

`tool_discovery` was added **2026-07-27** in `2609ed4 "Add deterministic lazy tool
discovery"` — 473 insertions, 201 lines of tests, authored by `js commit agent`, three-word
commit message, no body. It is wired into the live turn path at `runtime.py:1099` and has
15 passing tests.

It supports `kind` ∈ `{native, skill, mcp}`. Loading a native tool makes its full schema
available for the rest of the turn. Catalog entries are auto-derived:

```python
tool.description.split("\n", 1)[0][:240]   # registry.py:200
```

That is Claude Code's `searchHint` idea without a second string to maintain, and deferred
tools cost **zero** boot bytes because the catalog is returned by a call, not shipped.

### Why it has done nothing for six weeks [VERIFIED]

**The eager/lazy split is decided by module membership**, not cost or frequency:

```python
_LAZY_SUITES = {
    **{tool.name: "browser"  for tool in browser.tools()},
    **{tool.name: "terminal" for tool in terminal.tools()},
    **{tool.name: "wiki"     for tool in wiki.tools()},
}
```

That defers six of the cheapest tools (~5.7 KB total: `browser_probe` 1745,
`terminal_session` 1913, `terminal_snapshot` 914, three `wiki_*` under 400 each) and ships
all eight of the most expensive ones eagerly (`kernel` 3371, `fs_search` 3300, `browse`
3226, `fetch` 3028, `patch` 2859, `task` 2807, `toolbox` 2787, `ast_search` 2750).

`browse` is eager because it lives in `search.py`, not `browser.py`.

The lookup is already per-tool — `_LAZY_SUITES.get(tool.name)` — so this is a dict edit,
not an architecture change.

### It has been silently active in seven agents [VERIFIED]

Any agent selecting a lazy tool gets `tool_discovery` auto-injected
(`include_discovery = bool(self._lazy or self._skills or self.mcp_host is not None)`), and
those tools are withheld from the prompt:

```
coder          browser_probe terminal_session terminal_snapshot
defaultagent   browser_probe terminal_session terminal_snapshot
research       browser_probe terminal_session terminal_snapshot
reviewer       browser_probe terminal_session terminal_snapshot
triage         browser_probe terminal_session terminal_snapshot
wiki-flow      wiki_commit wiki_convert wiki_finish_ingest wiki_log wiki_write
wiki-ingest    wiki_convert wiki_finish_ingest wiki_write
```

A model in `coder` that reached for `terminal_session` got
`ERROR: no tool named terminal_session; use <the loaded set>` — because
`runtime.py:743/870/896` builds that message from `active_registry.names()`, which excludes
deferred tools. **The error tells the model the tool doesn't exist.**

### The retrieval model is the real gap [VERIFIED]

Nothing announces the catalog. The only boot-prompt hint is `tool_discovery`'s own
description ("Search the compact catalog of tools and skills allowed for this agent"),
which never says what's in it or that other tools exist.

`discover()` does substring-AND over `id + name + description + source`. No synonyms, no
stemming. Live test against the 27-tool config:

```
read a file  → read          run command  → shell
write file   → write         execute python → kernel
search       → 7 results     web          → 4 results

edit         → 0 results     (patch exists)
bash         → 0 results     (shell exists)
delete       → 0 results     (remove exists)
rename       → 0 results
download     → 0 results     (fetch exists)
find text    → 0 results
grep         → ast_search    (wrong tool; fs_search is the grep)
```

`patch` never says "edit". `remove` never says "delete". `shell` never says "bash".
Accurate descriptions, useless search keys.

### Costs, measured [VERIFIED]

```
bare discover()          31,582 B   92 results (30 native + 62 skills)
discover(kind=native)     5,772 B   30 results
discover(query="file")    4,500 B   14 results
discover(query="python")    210 B    1 result
load="native:read"       →  {"id":"native:read","loaded":["read"]}

name + first-line gist for all 28 tools:  2,049 B
names only:                                 279 B
```

### Recommendation

Putting `discover(kind="native")`'s output — ~5.8 KB, all names + gists — directly in the
boot prompt removes the search problem entirely: the model sees the menu and loads by
exact id. Boot goes ~1.2 KB → ~7 KB instead of ~28 KB, search never has to work, and the
two-round-trip cost disappears because discovery is only needed to *load*, not to *find*.

Also worth fixing: make `no tool named X` search the catalog, so a correct guess becomes a
load instead of a lesson that the tool doesn't exist.

### Two things that then bite

- **Loading mutates the prompt.** `registry.py:41` re-renders every description against
  the *current* surface, and `TurnToolSurface.tools` grows on load. Loading
  `browser_probe` silently edited `browse`'s description mid-session. Under heavy
  deferral that busts the cached prefix on every discovery call. Either freeze
  conditionals at boot against the full registry, or drop `{{#if}}` for deferred tools.
- **First use costs two round trips.** `test_discovery_cannot_authorize_another_call_from_same_response`
  pins that you can't discover and call in one response. Alternative is auto-load on first
  call by name — costs the safety property, buys the latency back.

---

## 6. Description bytes

Measured on the flat pre-variant layout (the stale `~/src/js` clone) — **these need
re-running against `~/js`, which renders from `stock/` or `slim/`.**

```
Full registry:  42,304 B descriptions + 13,709 B schemas
Worst offenders (rendered desc / schema):
  fs_search  3300 / 2315      patch      2859 / 1165
  kernel     3371 /  254      browse     3226 /  515
  task       2807 /  564      ast_search 2750 / 1058
  fetch      3028 /  420      toolbox    2787 /  387
  write      1264 /  369      read        493 /  624
```

Actual variant sizes on `~/js` [VERIFIED]: `stock/` 51,227 B, `slim/` 27,931 B, 28 files each.

Where the fat is, per the audits:
- `Parameters:` blocks in `.md` duplicating the schema — ~6.2 KB across 11 files
- `fetch.md` "Behavior" — 12 bullets of internals the model can't act on, ~1.6 KB
- `kernel.md` — three paragraphs of motivation
- `ast_search.md` worked examples — ~1.1 KB, one of them malformed
- `fs_search` schema — 717 B is six rg-style alias spellings
- `ast_search` schema — ~700 of 1058 B is the 27-language `lang` enum

**Comparison corpus** (`~/oldinbox/tool-comparison`): Claude Code exposes 36 tools in
14,464 description bytes; js exposes 30 in 45,773. Claude Code's distribution is skewed
(BashTool 5,500; FileReadTool 38; median 148) — js's is flat (median 1,180, cheapest tool
still 130). Breadth is cheap; uniform density is what costs.

---

## 7. Benchmark results

Six-run matrix; five completed before the driver died. **Solve counts are floors, not
measurements** — a large share of failures are the unwinnable tasks from 3.2.

### Tool calls, recomputed from session transcripts [VERIFIED]

| model | variant | sessions | calls | errors | rate |
|---|---|---:|---:|---:|---:|
| deepseek-v4-flash | slim | 12 | 248 | 13 | 5.2% |
| deepseek-v4-flash | stock | 12 | 246 | 11 | 4.5% |
| qwen3.8-27b | slim | 12 | 253 | 6 | 2.4% |
| qwen3.8-27b | stock | 12 | 281 | 10 | 3.6% |
| qwen3.8-flash-next | slim | 4* | 75 | 5 | 6.7% |

\* partial — that run had 10 `internal_error` of 12 tasks. qfn-stock never ran.

### Calls by tool

```
deepseek slim   shell 139  read 45  patch 38  fs_search 21  fetch 5
deepseek stock  shell 136  read 52  patch 35  fs_search 21  fetch 2
q27 slim        shell 109  read 60  fs_search 37  patch 31  fetch 16
q27 stock       shell 145  read 59  patch 40  fs_search 27  fetch 10
qfn slim        shell 42   read 19  patch 8   fetch 3  fs_search 2  remove 1
```

### Findings

**`patch` is the failure point in every run.** deepseek 6–7 errors, q27 5–7, and it's the
only tool that fails consistently. `shell` and `fs_search` essentially never fail. The
errors are the toolkit's own known bugs: the read-before-edit guard that `patch.md`
doesn't document, and `read` rejecting `[1, 180]` because the schema demands a nested
`range` object.

**Slim vs stock is a null result.** deepseek: 248 vs 246 calls, 5.2% vs 4.5% error. q27:
253 vs 281 calls, 2.4% vs 3.6%. Everything inside run-to-run noise on n=12, one seed.
**Halving description bytes (51,227 → 27,931) changed nothing measurable.** That is the
answer the slim set was built to get.

**Five tools out of 27.** Across every run the models used `shell`, `read`, `patch`,
`fs_search`, `fetch`. Never touched: `write` (not once), `ast_search`, `undo`, `remove`
(once, by flash-next), `todo_*`, `plan`, `task`, `skill`, `kernel`, `toolbox`, `browse`,
`browser_probe`, `terminal_*`, all search adapters, `tool_discovery`.

That is a property of the task shape, not the models. "Recreate this historical bug-fix
commit" needs find/read/edit/run/git and nothing else. **This bench cannot exercise the
tool system**, and in particular it cannot say anything about the deferral design, because
with everything eager `tool_discovery` never fires.

To actually cover the toolkit you need roughly one task archetype per cluster: a web-
grounded task, a stateful-compute task, an interactive-process task, a multi-turn planning
task, an undo/remove task, a structural-refactor task. Then the same tasks on
`lazy-everything` vs eager is a real A/B for progressive disclosure.

### Models do not cheat [VERIFIED] [I WAS WRONG]

I called test-file edits "tampering" three times. Reading the full trace killed it.
deepseek on click task-001, reasoning verbatim:

> *"Wait — but the instructions say 'Do not delete or weaken tests.' The historical commit
> deleted a test `test_fish_multiline_help_complete`... This is a tension: RepoRacer
> compares my patch with the historical human patch... The guidance here is likely more
> generic boilerplate... The simplest faithful reproduction is the exact historical diff."*

It then verified twice:

```
git diff b7e5fd4cc7de70280eccd39f2bb956df617e5519 -- <both files>
exit=0
```

Empty diff — **byte-identical to the target commit**. It noticed a stray trailing blank
line, inspected the file with `od -c`, and removed it to make the match exact. 53 tests
passed, then the full suite passed. Final stat `14 insertions, 65 deletions` — same as the
human's. The grader recorded `internal_error`.

That is a perfect score marked as a failure. The `-65` I kept citing as evidence of
cheating is the model matching the human patch exactly.

---

## 8. Naming

- **`toolkit/toolbox.py`** — persistent agent-authored tools living inside the package
  named `toolkit`. Highest-value rename (`workshop`, `forge`, `bench`).
- **Five things called "tool":** `tools/` (top-level, holds `envctx.c` and a wiki binary),
  `js/tools.py` (37-line compat shim, appears vestigial), `js/toolkit/`,
  `js/tool_args.py`, `js/tool_binaries.py`.
- **Six naming conventions in the model-facing tool names:** bare verb (`read`, `browse`),
  bare noun (`kernel`, `skill`), domain_verb (`fs_search`, `todo_write`), tech_verb
  (`ast_search`), **vendor_verb** (`serper_search`, `tavily_search`, `exa_search` — three
  tools named after companies), noun_noun (`terminal_session`, `tool_discovery`), and
  verb-in-the-middle (`wiki_finish_ingest`).
- **Three overlapping web tools with three naming styles:** `fetch`, `browse`,
  `browser_probe`. A model choosing between these is guessing.
- **31 slash commands**, more surface than js has tools. `/model` and `/pick-model` call
  the identical function (`_pick_model_into_state`). `/models` is really `--list`.
  `/refresh-model-catalog` is really `--refresh`. Also `/exit` `/quit` `/q`; and
  `/reset` (clear memory, keep jsonl) vs `/wipe` (rotate jsonl) vs `/flush` (drop queued
  prompts — unrelated, just sounds similar). `/codex`, `/callback`, `/v1` don't appear in
  `cli.py`, `tui.py`, or `setcmd.py` at all.
- **Singular/plural** is *not* a widespread problem. One env pair exists
  (`JS_TOOL_DESCRIPTIONS` canonical, `JS_TOOLS_DESCRIPTIONS` accepted alias in
  `_VARIANT_ENV_NAMES`) and it's deliberate. Only real oddity is
  `limits.max_tool_result_bytes` next to `limits.max_tool_results_per_turn_bytes`.
- **`js/js/`** — repo dir and package share a name, so every path reads `js/js/toolkit/…`.

---

## 9. Audit agent scoring (for the model eval)

Four agents audited the same tree. Prompt required code snippets + file + line numbers for
every "broken" claim, and a `GREENFIELD_PLAN.md` with zero references to js.

| | js-g | js-pt | js-qb | js-q (chat) |
|---|---|---|---|---|
| code fences in audit | **0** | 52 | 16 | many |
| cited line numbers valid | **0 / 7** | 31 / 31 | 86 / 88 | mixed, drifts by 100s |
| js refs in GREENFIELD | **9** | 1 | 5 | n/a |
| measurements | none | ran tooldiag | ran tooldiag | ran tooldiag |
| repo left clean | yes | yes | yes | yes |

**js-g failed the evidence standard outright.** 33 "broken" bullets with zero code
snippets, all seven sampled line numbers wrong with error growing monotonically with file
depth (signature of estimating positions rather than reading them), at least five bullets
whose own text concludes the issue is fine, and a High-severity fix
(`shutil.rmtree(followlinks=False)`) that is a fabricated API — `rmtree` has no such
parameter and refuses directory symlinks rather than following them. Applying its top
recommendation would break `remove`.

**js-pt and js-qb are comparable.** js-qb's per-tool byte table was byte-identical to
js-pt's, independently corroborating both. js-qb's sharpest find was the CRLF machinery
being structurally dead; js-pt's was undo deleting a file on snapshot-capture failure.
js-qb also ran a control check (verified the mitigation works before showing the hole),
which is the discipline the others lacked.

**js-q had the widest reach** — it found the `patch` `oneOf`/grammar issue, the
schema-rejects-what-handler-accepts *class*, and the `tooldiag` total bug — and paid for it
with citation drift.

Cheap mechanical scoring that separates them without judgment: count code fences, grep the
greenfield doc for project nouns, and diff cited line numbers against `grep -n "def "`.

---

## 10. Current state

```
~/js   branch tools-1
  f4ce13e  Let the tool bench reach a public https endpoint and set reasoning effort
  63f7610  Flatten patch's schema so grammar-constrained models can call it
  f871710  Let the subagent registry spy accept the descriptions kwarg  (pre-existing)

  MODIFIED, uncommitted:  js/toolkit/registry.py   (lazy-everything experiment)
  untracked:  .scratch/bug-patch-edits-array-serialized-as-string.md, graphify-out/
```

`~/.config/js/agents/defaultagent/00-tools.yaml` was rewritten from 16 to 27 tools
(previous saved as `00-tools.yaml.bak`). Added: `browse`, `kernel`, `toolbox`, `todo_read`,
`todo_write`, `plan`, `task`, `skill`, three `wiki_*`. Deliberately excluded `commit`,
`defaultagent`, `twotool` — those are generated per-prompts-root by `_agent_tools()`, and
listing `defaultagent` inside defaultagent lets it spawn itself. `tool_discovery` is not
listed because `runtime.py:46` reserves the name and injects it.

`~/src/js` is a stale GitHub clone from 2026-09-08 missing the four unpushed 2026-09-05
commits. It has no value now. Delete it.

Benchmark results survive in `~/js/bench/toolbench/results/` for stamps `20260909T214450Z`
(deepseek both arms), `221108Z` (q27 slim), `223557Z` (q27 stock), `231104Z` (qfn slim,
partial). The `/tmp` work dirs and matrix driver script are gone.

---

## 11. Priority order

Contract fixes before byte trimming, so the trim doesn't preserve descriptions of behavior
that doesn't exist.

1. `read` — add `start_line`/`end_line` to the schema, or change the continuation footer to
   emit `range={...}`. One line either way; currently blocks every paginated read.
2. CRLF — either read/write bytes so preservation is real, or delete the claim from
   `write.md` and have `patch` report "line endings normalized to LF". Don't leave the diff
   lying.
3. `patch` — pick one contract for `path`/`search`/`content` and make schema, `patch.md`,
   and the handler agree.
4. Bench miner — reject commits whose test change is net-negative (3.2). Until then every
   benchmark number is contaminated.
5. `hide_files` — make it actually strip the file list, or delete the setting (3.1).
6. `_LAZY_SUITES` — decide the eager set by cost/frequency, not module. Then put the native
   catalog in the boot prompt.
7. `tooldiag` — total on rendered + schema, keep raw as its own column (G).
8. Then trim: `Parameters:` blocks (−6.2 KB), `fetch.md` Behavior (−1.6 KB), `kernel.md`
   motivation, `ast_search.md` examples, `fs_search` aliases (−717 B), `read` flatten.
9. Rename `toolkit/toolbox.py`; scrub `fs_read` (non-existent tool) and the `KING` persona
   name from portable tool contracts; fix the `12:ab|` anchor format in
   `docs/tools-reference.md` and `ast_search.md`.

**Test that would have caught most of this:** a property test that replays each tool's own
hint strings and error messages through its own published schema. Nothing currently asserts
that a schema-legal call runs, or that a schema-rejected call is one the handler supports.
