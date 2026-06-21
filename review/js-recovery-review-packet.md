# js recovery and hard-review packet

Generated read-only after the branch consolidation on 2026-06-19. No code/docs changes were applied as part of this packet.

> RESTORED 2026-06-21 from the session log after the original /tmp copy was lost in a crash. Verbatim.

## Current repo state

- Repo: `/home/admin/js`
- Current local branch: `main`
- Current local HEAD: `f88f888 merge: consolidate main and fix branch histories`
- Local `main` and `fix/tool-args-repair-asymmetry` both point at `f88f888`.
- Nothing was pushed.
- One unstaged working-tree line remains in `README.md`: `FIXME: fs_read seems pretty live to me?`

## Immediate answer: `fs_read`

The FIXME is legitimate.

- `fs_read` is live as an internal Python implementation symbol: `js/toolkit/fs.py` defines `def fs_read(...)` and registers it as the handler for public tool `read`.
- `fs_read` is not a model-facing/public tool name. The registered tool name is `read`.
- Current README wording calls `fs_read` a legacy alias, which is imprecise and self-contradictory.
- `tests/test_docs_guidance.py` currently forces README to keep that questionable wording. That is a doc-test freezing a contested claim.

Recommended owner decision: replace the README sentence with a precise statement about public tool names, and replace/delete the prose-pinning test. If a guardrail is needed, test the registry: `fs_read` must not be a registered public tool name, while `read` remains registered.

## Recovered/lost-history map

### Reflog/unreachable commits in `/home/admin/js`

- `7672151` — pre-amend `Add js: terminal LLM agent harness`; only delta to current amended import is `.gitignore`.
- `224e869` — stash/WIP `On main: ape-baseline`; mostly corresponds to later ruff/tooling commits, but differs in 5 files and should be reviewed only as historical context.
- `2ef5507` — WIP on `fix/tool-args-repair-asymmetry`; mostly became `d6b1da2`, with later branch changes explaining remaining differences.
- `7aa078a` — rescue WIP; contains the current README FIXME line.

No full vanished two-day commit stack was found inside the current repo object database.

### Other local workdirs

Found local recovery sources under `/home/admin/work`:

- `newest_js/` — code changes are represented in current main; useful provenance docs under `docs/investigations/` and `handoff/` only.
- `js_abby_dooby/` — history represented by `peeled/`; dirty changes are older/superseded; no code rescue recommended.
- `bench_js/` — represented by `peeled/`; staged deletions correspond to old cleanup patch; do not apply wholesale.
- `js_test`, `js_test2`, `js_test3`, `js_test4` — playground variants of docs/prompt/config experiments captured by `peeled/`; no unique code rescue found.
- `/home/admin/work/peeled/` — best structured reconstruction source: 36 patch files plus skipped changelog-only commits.

## `/home/admin/work/peeled` classification

### d01-d12 docs/prompt/config early patches

- d01 Cover layered prompt precedence — SUPERSEDED. Keep invariant; do not apply old patch wholesale.
- d02 Clarify prompt precedence in top-level docs — SUPERSEDED. Keep project > global > repo invariant; avoid exact-string doctrine tests.
- d03 Document model env precedence in README — DISCARD. Mentions removed `ME_MODEL` alias.
- d04 Document provider extra in top-level guidance — DISCARD. `provider.extra`/LiteLLM raw kwargs are obsolete.
- d05 Document append-only compaction in README — SUPERSEDED. Behavior already documented.
- d06 Document Claude provider name boundary — DISCARD. Current runtime uses configured alias profiles, not automatic `claude` substring behavior.
- d07 Document canonical tool names in README — DISCARD/REWRITE. Pink-elephant README doctrine; conflicts with live internal `fs_read` handler name.
- d08 Document runtime layout in top-level docs — SUPERSEDED. Keep platform-data wording, not old `~/.js` wording.
- d09 Clarify runtime state paths in CLI help — SUPERSEDED.
- d10 Ignore personal debug files — REVIEW WITH OWNER. Broad personal ignore patterns may be acceptable but should be chosen consciously.
- d11 Inline directive expansion — REVIEW WITH OWNER. Feature is useful; current main has later fixes, but subagent expansion path needs review.
- d12 Layout/config/agents/inline directive docs — REVIEW WITH OWNER. `docs/agents-and-prompts.md` may be a real missing artifact; restore only with current platformdirs/ai-python wording.

### d13-d25 provider/model spine patches

- d13 temporary pending LiteLLM gutting — DISCARD.
- d14 `shite` handoff removal — DISCARD.
- d15 provider login/model picker — SUPERSEDED.
- d16 local provider shortcuts — SUPERSEDED.
- d17 Codex OAuth provider — KEEP CONCEPT / SUPERSEDED by current codex files.
- d18 provider spine rebuild — REVIEW WITH OWNER. Rescue selected architecture only; do not apply raw patch because it includes scratch/session artifacts.
- d19 JS_REASONING — KEEP/RESCUE; present in current.
- d20 models.dev limits — KEEP/RESCUE; present in current.
- d21 models.dev auto refresh — KEEP/RESCUE; present in current.
- d22 provider login registry flow — REVIEW WITH OWNER; custom login runtime routing still needs design.
- d23 opencode-go model filtering — KEEP/RESCUE.
- d24 restrict picker to logged-in models — SUPERSEDED by d25; do not use without d25 due credential-routing issue.
- d25 provider picker routing leaks — KEEP/RESCUE; important.

### d26-d36 commit/cleanup patches

- d26 TODO tracking pre-existing failures — DISCARD.
- d27 quiet flag — SUPERSEDED; current CLI has it.
- d28 remove `remove` from commit agent tools — SUPERSEDED.
- d29 XDG config test alignment — SUPERSEDED.
- d30 import `call_tool` from toolkit.core — SUPERSEDED.
- d31 quick-do-shit agent mode — DISCARD. Dangerous doctrine: skip verification/provenance and dump deferrals to ignored TODO.
- d32 changelog/commit policy in CLAUDE — SUPERSEDED.
- d33 teach commit agent meaningful changelog entries — SUPERSEDED but intent remains useful.
- d34 remove stale scratch/specs/old lock — REVIEW WITH OWNER; manual cherry-pick only. Do not delete planning artifacts or `uv.lock` wholesale.
- d35 reorganize `.gitignore` — SUPERSEDED; only cherry-pick missing ignore patterns after audit.
- d36 update `uv.lock` — DISCARD; regenerate lock from current `pyproject.toml` instead.

Skipped changelog-only entries in `SKIPPED.txt` should not be applied wholesale; rewrite only if owner wants historical narrative preserved.

## Hard-review findings: current main

### Error / high-priority

1. `js/toolkit/fs.py` remove follows symlinked directories.
   - Risk: removing a repo-local symlink to a directory can recursively delete the target outside the repo.
   - Fix shape: use `lstat`/symlink-aware handling; unlink symlink itself or refuse symlinked directories.

2. `js/toolkit/wiki/helpers.py` vault inference can fall back to `creative` after removing bare `inbox/` marker.
   - Risk: ingest from a fresh/non-`wiki-*` vault with `inbox/` but no `PURPOSE.md` can operate on the wrong vault.
   - Fix shape: fail closed when no vault inferred, or require stronger vault-shape markers.

3. `js/toolkit/meta.py` subagent model override only replaces `cfg.model`.
   - Risk: `model: openai/gpt-5` can be sent through the parent provider/base/api/header route.
   - Fix shape: resolve subagent model through the same provider routing as top-level model selection.

4. `js/commit_helper.py` survey hides already-staged hunks when a file has both staged and unstaged changes.
   - Risk: commit agent can commit staged work without review or mix logical units.
   - Fix shape: show cached and unstaged hunks separately, or refuse pre-staged state.

### Warning / medium-priority

5. `js/toolkit/process_net.py` fetch accepts non-HTTP schemes through `urllib`.
   - Risk: `fetch(file://...)` can read local files despite HTTP/HTTPS docs.
   - Fix shape: parse URL and allow only `http`/`https`.

6. `js/toolkit/core.py` read-before-write guard records hashes but does not reject stale reads.
   - Risk: external/user edits after a read can be overwritten from stale context.
   - Fix shape: re-hash before write/patch/remove and force a fresh read if changed.

7. `js/persona.py` prompt sampling writes into process-wide `os.environ`.
   - Risk: one persona's sampling leaks into later personas/subagents/threads.
   - Fix shape: carry sampling in config/request state, not global env.

8. `js/model_client.py` sampling params are provider-agnostic.
   - Risk: Anthropic/Minimax-style transports can receive unsupported OpenAI knobs like `presence_penalty`.
   - Fix shape: gate/translate per provider transport.

9. `js/toolkit/meta.py` AGENTS prepending drops prompt frontmatter fields.
   - Risk: worker model/sampling/secondary_model lost when AGENTS files are present.
   - Fix shape: preserve PromptSpec fields when rebuilding system text.

10. `js/toolkit/meta.py` locked subagent model flag not propagated to nested worker registries.
    - Risk: nested workers can still see `model` in task schema even when parent lock says no.

11. `js/runtime.py` artifact config is not applied to active tool context.
    - Risk: `[artifact] dir/url/bin` docs/settings exist but tools still use defaults/env.

12. `js/toolkit/wiki/helpers.py` configured wiki aliases are advertised but hard-coded resolver ignores them.

13. `js/runtime.py` `_canonical_tool_args` preserves double-encoded JSON strings as valid raw JSON instead of canonical object args.

14. `js/commit_helper.py` untracked `stage <file> 1` stages the whole file instead of requiring `all`.

15. `js/commit_helper.py` survey can report clean tree when Git failed because `_git` ignores `check`.

## Docs/tests/prompt doctrine findings

### Strong delete/loosen candidates

- `tests/test_docs_guidance.py::test_top_level_guidance_rejects_legacy_tool_aliases`
  - Freezes questionable README `fs_read` wording.
  - Replace with registry behavior test or delete.

- `tests/test_docs_guidance.py` substring ban: `assert "migrat" not in text.lower()`.
  - Panic footgun; blocks legitimate "migration/migrate" wording.
  - Delete.

- Many docs tests assert exact sentences and embedded line breaks.
  - Keep tests for public tokens/paths/flags; remove prose pins.

- `prompts/commit/01-prompt.md` contains repeated negative constraints and pink-elephant wording.
  - Rewrite into positive procedure only after owner review; prompt behavior is load-bearing.

- `handoff/HANDOFF.md` stores interpersonal incidents as doctrine.
  - Do not promote to docs. Archive/delete after owner review.

### Real docs drift to fix

- `docs/config.example.toml` says shown values are built-in defaults but contains tuned overrides (`max_output_tokens=65535`, `reasoning_effort="high"`, `max_tool_iterations=99`, etc.). Decide whether it is a true default template or a personal tuned preset.
- `limits.jsonl_max_line_chars` and `JS_JSONL_MAX_LINE_CHARS` exist in code but are missing from docs/configuration and config.example.
- `[subagents] prefer_inherit` and `lock_model` are consumed by code but absent from `_KNOWN_KEYS`, generated template, config.example, and config docs.
- `compact.auto` and `compact.model` are consumed but missing from `_KNOWN_KEYS`/generated template.
- Changelog/docs use both `lock_subagent_model` and TOML `lock_model`; public TOML key needs one clear name.
- README/docs link to missing `docs/agents-and-prompts.md` and `docs/porting-forge-tool-system-to-python.md`.
- `docs/user-guide.md` contains flippant default model wording: "what I feel like it this week". Replace with actual default or remove.
- Docs mention nonexistent `autocoder` examples while current prompt roots only include `defaultagent` and `commit`.

## Scratch artifact triage

- `TODO.md` — mostly overtaken by merged model-selection work. Remaining true gaps: top-level `--agent` does not apply agent frontmatter `model`; prompt files do not populate new model/sampling frontmatter; no per-task pinning.
- `handoff/HANDOFF.md` — stale and high tone inflation. Four of five NOT DONE items are done; only top-level `--agent` frontmatter model application remains real.
- `handoff/plan-blocks.md` — unrelated cross-contamination from another project; do not treat as js repo guidance.
- `handoff/commitbot-audit/*` — useful as reference for pink-elephant prompt cleanup; not source of truth.
- `banks/js-*/mnemopi.db*` — private agent memory DB; not inspected; keep unless owner wants state cleanup.

## Suggested review order for owner

1. Safety/runtime blockers: symlink remove, fetch scheme restriction, stale read hash, wiki/artifact config routing.
2. Provider/model correctness: subagent provider routing, sampling isolation, sampling provider support.
3. Commit workflow: commit_helper staged/unstaged survey, untracked `all`, prompt integration.
4. Config docs/settings surface: `_KNOWN_KEYS`, generated template, config.example truthfulness.
5. Docs tests: delete/loosen prose pins before rewriting docs.
6. Prompt doctrine: commit prompt and defaultagent prompt, with owner review before behavior changes.
7. Lost-history patches: only use `peeled/` as historical reference; do not apply wholesale.

## Recommended operating mode from here

- Freeze feature work until safety/runtime blockers are fixed or consciously deferred.
- Do not mass-apply old patches.
- Make small commits grouped by real behavior: safety fixes, config-surface fixes, docs-test de-doctrining, prompt cleanup.
- For prompt cleanup, prefer owner-reviewed diffs because prompt constraints are behavioral code.
