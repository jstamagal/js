# js — Implementation Plan

Built from TODO2.md (the decision record) and hardened over three gpt-5.5 review passes (critique → refine → critique → refine → confirm; final verdict: GO). Ordering honors the one hard call: the config/`set` system is FIRST. Test for every item: cleaner / more modular / more honest — runway toward js becoming its own scripting language.

---

## NORTH STAR (destination, not built now)
js becomes its own ircII-style, strings-only scripting language: `/`-commands through a lexer; slashless in script files; `set` for settings, `/assign` for variables (local+global); verbs `/IF /FE /FEC /ON /ALIAS /EXEC /EVAL /ECHO`; `/ON` binds a typed internal event bus; `/load` + scripts-load-scripts; config files ARE scripts; js is a script loader; extend js by script, no Python edits.

## GUARDRAILS (apply to every phase)
- **One config mechanism.** No permanent dual TOML+set; migration is bounded with a deletion date.
- **Shared command runner now** (set + show, no expansion). Do NOT bolt more prefix checks onto `cli.py:_handle_command`.
- **No `os.environ` mutation** from prompt loading or CLI flags; sampling/model flow as typed objects.
- **No string-match provider/model routing**; route by provider transport/capabilities.
- **Runtime knobs (set-script) ≠ agent manifests (YAML→typed objects).**
- **Phase-local docs/tests ship with their phase.** Phase 6 is residual only.

---

## PHASE 1 — Config foundation: knob registry + `set` runner  (FIRST)
1. **Discovery.** Live `set` today = `KNOBS={debug,reasoning,maxout}` in `js/cli.py` mutating REPL `state`; real surface = `settings._KNOWN_KEYS` + `config.Config` + raw `cfg.settings` consumers (`compact.*`, `tools.alias_profiles`, `subagents.*`).
2. **One knob registry (`SettingSpec`).** Per knob: key, dotted storage path, type/coercer, default, empty-state display (`off`/`<none>`/`<unset>`), mutability, doc text. Generate `set` parsing, `show`, the stock config, docs, AND tests from this one registry. Audit at least: `_KNOWN_KEYS`, `compact.auto/model/summary_max_tokens`, `subagents.prefer_inherit/lock_model`, `tools.alias_profiles`, wiki aliases, artifact fields, sampling knobs.
3. **Minimal shared command runner.** Lex a line into words; normalize `/set` (REPL) vs slashless `set` (script files); dispatch `set` + `show`; NO expansion yet. Config loading AND the REPL both call this runner. (`/assign`, `/eval`, aliases, scope, expansion = later language phase.)
4. **Config-as-script + TOML cutover — exact contract:**
   - **Filenames (`rc` convention, after `.ircrc`/bitchtea `bitchtearc`):** global `~/.config/js/jsrc`; project `<repo>/.js/jsrc`; local `<repo>/.js/jsrc.local`. First-run template → `~/.config/js/jsrc`.
   - **Layer precedence (low→high):** built-in defaults → `~/.config/js/jsrc` → project `.js/jsrc` → `.js/jsrc.local` → operator env (`JS_*`) → CLI one-shot override.
   - **Hard cutover.** `config.from_env` reads only `jsrc`; remove TOML loading. One-shot `js --migrate-config` reads an existing `config.toml` once, writes the equivalent `jsrc`, warns; removed after 2 releases (deletion date in changelog).
   - **Delete `docs/config.example.toml`** — the registry generates a stock `jsrc` and the docs.
5. **`show`-all view:** every knob + current value, honest empty states.
6. **Stock defaults** as set-lines (provider/model/baseurl/effort + `wiki.aliases.creative/general`).
7. **Agent manifests are SEPARATE:** `00-tools.md` → `00-tools.yaml` per agent dir; fields `tools`, `model`, `secondary_model`, `sampling`. Prompt text stays in `01-prompt.md`. Model/sampling frontmatter moves out of markdown into `00-tools.yaml` (bounded migration reader if an old `00-tools.md` is found; same 2-release sunset). Only runtime knobs are `set` keys; agent manifests load into typed `PromptSpec`/agent objects.
- **Docs/tests ship HERE:** rewrite `configuration-and-sessions.md`, delete `config.example.toml`, replace `tests/test_settings_toml_loader.py` with `jsrc`-loader + layer-precedence + registry-roundtrip tests, fix the first-run template test.

## PHASE 2a — Config→runtime plumbing
- **Artifact config:** `Config` lacks `artifact_dir/url/bin` — add the fields, load from settings into `Config`, then copy onto `ToolContext`. Precedence config → `ARTIFACT_*` env → default.
- **Wiki aliases + fail-closed:** remove the hard-coded `VAULT_ALIASES` dict; wire `resolve_vault` to config aliases. Missing vault → fail closed (`cd`/`--vault`; inference only `PURPOSE.md`/`wiki-*`).
- **Acceptance (all paths):** `run_turn` copies artifact fields; `_run_artifact` builds a configured `ToolContext`; `resolve_vault` honors config aliases; `infer_vault` fails closed (no silent `creative`). Docs/tests ship in 2a.

## PHASE 2b — Provider route resolver + per-wire sampling
- **`resolve_model_route(base_cfg, requested)`** → `{model, provider_id, base_url, api_key, headers, transport/wire}`. Replaces all prefix/provider parsing at `config.from_env`, `cli._cfg_for_active_model`, subagent selection, and compaction. `model_client.resolve_model` stays ONLY the final ai-provider factory.
- **Sampling/env cleanup (PREREQUISITE):** remove `os.environ` mutation in `persona._apply_sampling_env` and the `JS_*` writes; model calls take a typed `sampling` object. **Precedence (low→high):** set-script defaults → agent manifest (`00-tools.yaml`) → operator env (`JS_*`) → CLI/live override. `JS_*` read only at config-load boundary.
- **Per-wire sampling via a transport capability map** keyed by `ProviderDef.transport`/wire: allowed top-level params, allowed `extra_body`, reasoning form, history/append-only policy. Default send-nothing. Tests: Anthropic-wire drops penalties; OpenAI-wire accepts expected; no leak between agents.

## PHASE 2c — Agent/subagent model selection (on the resolver)
- Subagent frontmatter `model:` survives AGENTS files (preserve `PromptSpec` fields when prepending).
- `lock_subagent_model` actually removes the `model` arg from the task tool (wire into registry/flags); honest doc.
- Top-level `--agent` applies that agent's `model` (through the resolver).
- Show the per-subagent `model` override by example in the built-in agents.
- **Docs/tests in 2c:** model-precedence (tool-call > inherit-if-prefer > frontmatter > parent), AGENTS-present survival, lock drops the arg, `--agent` applies model.

## PHASE 3 — Commit bot (decoupled from inline-code; parallel to Phase 2)
1. **Promote `commit_53`:** import its effective prompt into the repo commit-agent path (`prompts/commit/`, or global `agents/commit` if intentionally personal); delete the old `git add -p` prompt.
2. **Wiring (NOT coupled to inline executable prompts):** `_run_commit(target_dir)` runs/initializes the repo and injects a deterministic `commit_helper survey` snapshot; deterministic staging exposed as a commit-specific tool OR documented `python -m js.commit_helper stage <file> <hunks>` bound to `target_dir`.
3. **Helper fixes + binding:** show staged AND unstaged; untracked + spec other than `all` errors clearly; `_git` honors `check`; bind `commit_helper`'s implicit cwd to `target_dir`.
4. **`js --commit` just-works:** not a repo → auto `git init` (local, no remote); handle messy git states (empty repo, detached HEAD, mid-merge/rebase, mixed staged+unstaged, untracked). Personal remote stays a `~/.config/js/agents/commit` override.
- **Tests:** `commit_helper` staged/unstaged + untracked + `_git`-check, target-dir binding, `_run_commit` auto-init, prompt references `stage` not `git add -p`.

## PHASE 4 — Fetch (after write-safety/path semantics + routing)
Whole-hog: web/API, `file://`, methods/headers/raw+JSON body, downloads, binary, image-for-vision, paste shortcut, CLI `-f/--file`, REPL `@file`. Split "network fetch" from the "image/file attachment path" so the attachment/vision side doesn't block provider work.

## PHASE 5 — Provider audits (AFTER the route/wire abstraction)
DeepSeek + MiMo, vLLM/local OpenAI-shaped. Acceptance = request/response EVIDENCE: exact params emitted per provider, history/tool-call behavior, DeepSeek cache-hit usage parsing + a visible %, local-server model/limit probing. No new model-name heuristics.

## PHASE 6 — Residual de-slop only
Delete tests that freeze behavior / police prose / ban substrings / impose ceremony; tool-visibility (agent only knows selected tools, descriptions in tools, no "cannot do" prose); leftover stale docs. Inlining stays approved.

## LONG-TERM — the language
On the Phase-1 runner: full lexer + verb set (`/IF /FE /FEC /ON /ALIAS /EXEC /EVAL /ECHO`); `/assign` + scope + spine-vs-in-function expansion; `/load` + slashless scripts + composition; typed event bus + `/ON` hooks; functions as param-bound aliases. Extend js by script, no Python edits.

---
### Genuinely trivial: symlink no-follow in `remove`. (`_git(check=...)` and artifact-config are NOT trivial — Phase 3 and Phase 2a.)
