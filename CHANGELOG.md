# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Provider validation at set time.** `set provider.id` now rejects unknown provider ids with a clear error; `set provider.base_url` rejects values without an http(s) scheme. Invalid env-var overrides (`JS_PROVIDER`, `JS_BASE_URL`) now print a warning to stderr instead of silently skipping.
- **Regression coverage for capped shell output.** Added a test proving the capped reader holds memory at the cap while draining to EOF, preventing OOM from runaway commands.
- **Regression coverage for byte-limited UTF-8 previews.** Added tests proving attachment sniffing and artifact previews survive when a byte cap splits a multibyte character.
- **Regression coverage for non-blocking state commands.** Added tests for detecting `/reset`, `/wipe`, and `/compact` commands that would mutate live turn state.
- **Regression coverage for drain staging collisions.** Added a test proving split text files with the same name in different inbox subdirectories keep distinct staged pieces.
- **Regression coverage for post-heal memory marks.** Added tests showing rollback and compaction markers keep the intended messages after orphaned tool calls are healed.
- **Regression coverage for OpenAI-compatible sampling extras.** Added tests proving `top_k` and `repetition_penalty` stay in raw `extra_body` while accepted sampling knobs remain structured.
- **Regression coverage for symlinked undo snapshots.** Added a test proving `undo` can restore a removed file when the removal path crosses a symlinked parent.
- **Regression coverage for wiki conversion edge cases.** Added tests for stale LibreOffice output and binary paths whose names contain the word `text`.
- **Non-blocking job controls.** Added `/jobs` and `/cancel [id]` REPL commands so `js --nonblocking` users can inspect running turns/subagents and cancel a specific job or the active turn without leaving the session.
- **Non-blocking job command tests.** Added command-level coverage for listing jobs, cancelling active turns or explicit ids, and handling invalid or missing targets.
- **Non-blocking REPL integration tests.** Added headless coverage for the async REPL path so a real queued turn persists messages and blank input exits cleanly without launching prompt_toolkit terminal machinery.
- **Experimental non-blocking REPL mode.** Added `js --nonblocking` so interactive input stays live while a turn streams on a shared async loop, with Ctrl-C cancelling the active turn instead of exiting the process.
- **Supervisor fan-out tests.** Added coverage for subagent fan-out through the live supervisor ramp, including cancelable job registration and per-task error reporting.
- **Supervisor job registry.** Added a single-loop supervisor for tracking, cancelling, and cross-thread scheduling turn and subagent tasks, with tests covering lifecycle cleanup and cancellation semantics for the non-blocking REPL work.
- **Non-blocking output event contract.** Added the `OutputEvent` data shape, stdout sink fallback, agent identity helper, and design notes for the future non-blocking/windowed runtime so output can move toward structured events without changing current default behavior.
- **Claude Code repo guidance.** `CLAUDE.md` now documents the local workflow, command entry points, architecture map, and project landmines so Claude-based agents start with the same operating rules as the rest of the harness.
- **Configurable inline prompt expansion timeout.** Inline code directives now default to 300 seconds and can be tuned with `limits.inline_code_timeout_s` or `JS_INLINE_CODE_TIMEOUT`, so slower prompt assembly commands can complete without hard-coded limits.
- **`js --login` model curation.** After fetching a provider's live model list, a spacebar checklist lets you keep only the models you want cached for `/model` and `--list-models` (all preselected, so a bare Enter keeps everything; `a`/`n` select/clear all). Each row is tagged with its wire dialect from models.dev `provider_config.npm` (e.g. `anthropic` vs `openai`), so a multi-endpoint gateway's anthropic-only models are visible at a glance. A free-text line adds ids the endpoint omitted. Falls back to caching the full list when there's no TTY (piped logins).
- **`--list-models [PROVIDER]`.** Print a concise `provider/model`-per-line list for every saved login (or one named provider), served from the model cache for offline speed, so external pickers and shell users can discover valid model flags without digging through JSON.
- **REPL Tab completion.** Tab completes by prefix (never fuzzy) with a rotating menu: commands at line-start (a slashless word gets an implicit `/`, so `comp`→`/compact`), `/set`/`/show` knob keys, `/login`/`/provider` names, file paths for `/…`/`@…` tokens, and hunspell spelling suggestions for prose words. Tab-triggered only (`complete_while_typing=False`).
- **`/login <name> [apikey] [baseurl] [provider]`.** Build and persist a named login inline — a custom name with an explicit transport (e.g. a local vLLM endpoint as `openai`), or a known provider that infers its own type. Bare `/login <name>` still loads a saved login.
- **`/compact-auto on|off`.** Real handler that toggles `compact.auto` live (it was advertised in help with no handler before).
- **`/load <file>` ircII-style script loading.** Load slashless command scripts from the REPL or nested within other scripts. Script files accept `set`, `show`, `load` (nested, resolved relative to the containing script), and `on` — the same command surface as the REPL minus provider commands. Cycle detection and a depth limit guard against runaway nesting.
- **`/on [event handler]` typed event hooks.** Register and inspect named event hooks against the runtime event stream. Canonical events include `turn_start`, `turn_end`, `prompt`, `stream`, `response`, `tool_call`, `tool_result`, `error`, and more. The `^` prefix marks a hook as suppressive. The runtime now emits these events at the appropriate lifecycle points and executes handlers through the slash/setcmd command surface (`set`, `show`, `load`, `on`). Handler failures are captured as event results/debug telemetry instead of aborting turns, and nested dispatch is guarded.
- **Ruff lint/format dev tooling.** Ruff lives in a `uv` dev dependency-group
  so `uv sync` puts it on the project venv PATH (agents can call `ruff check` /
  `ruff format` through the shell tool directly); config in `[tool.ruff]` (line
  length 100, `js/toolkit/wiki/prompts.py` excluded as a prompt-template
  builder). A `mypy` pass was tried and dropped — it flooded the dynamic
  codebase (ToolContext dynamic attrs, `**kwargs` splats, implicit optionals)
  with unactionable errors.
- **tok/s in the tool-call debug trace.** The `-d`/`--debug` per-call finish line now reports output tokens and tokens/sec (`{out} tok  {tps:.1f} tok/s`) alongside elapsed ms and tool-call count.
- **`JS_*` sampling overrides.** Pin sampling for a run without editing config: `JS_TEMP`, `JS_TOPP`, `JS_PRPEN` ride top-level (OpenAI-standard); `JS_TOPK`, `JS_REPPEN` ride `extra_body` (vLLM extensions) and merge with any provider extras (e.g. DeepSeek `max_reasoning_tokens`). Unset = send nothing and defer to the backend/model default. Swaps qwen coding (`0.6/20/0.95/1.0/0.0`) vs creative (`1.0/20/0.95/1.1/1.5`) presets per invocation.
- **`justfile` task runner over `uv run`.** `run`/`drain`/`commit` (pass-through), `sync` (rebuild env from `uv.lock`), `test` + focused suites + `test-file`/`test-mark`/`test-live`, `lint`/`fix`/`format`/`check`, `build`/`lock`/`upgrade`/`clean`/`shell` — replaces the `.venv/bin/js` + manual pip dance.
- **The `js` harness.** A terminal LLM agent you run as a bare command from any
  directory: an interactive REPL with slash commands and real arrow-key history,
  a one-shot `js -p "prompt"` mode, stdin piping (`echo prompt | js`), and
  `--no-save`/`-n`, `--agent`, `--session`, `--debug`, and continuation flags.
  Turns stream through a provider boundary that aggregates tool-call fragments as
  they arrive and classifies failures (retriable transport errors back off and
  retry; auth/bad-request errors abort the turn cleanly; a crash inside a tool is
  handed back to the model as a result so it can self-correct).
- **Persistent sessions.** Every turn is appended to a per-session JSONL file
  under an `fcntl` lock with `fsync`, schema versioning, and control marks, so
  two shells can't tear each other's lines and a crash mid-write can't leave a
  half-record. Sessions get unique timestamped ids, are load-only, and any past
  session can be reopened and continued.
- **Tool runtime and the canonical tool surface.** A dataclass tool registry
  that auto-derives each tool's schema, drops unknown arguments instead of
  crashing, and coerces mistyped ones. Guardrails include read-before-overwrite
  enforcement, anchored reads, exact patching, undo snapshots, and search
  deduplication. The core surface is `read`, `write`, `fs_search`, `sem_search`,
  `patch`/`multi_patch`, `undo`, `shell`, `fetch`, `followup`, `plan`, `skill`,
  `todo_write`/`todo_read`, and `task`.
- **Tool definitions rework.** Tool descriptions became model-facing `*.md`
  contract files, image and PDF reading was added, and tool parameters were
  reshaped to the Forge shape. (The transitional `fs_*` names were retired in the
  same pass; see Removed.)
- **Subagents via `task` and generated agent tools.** `task` spawns concurrent
  worker turns: every task string inside one call runs in a thread pool, and
  multiple `task` calls in one turn dispatch concurrently, with stable result
  ordering. Work is bounded by a concurrency cap, a per-job timeout, and a
  recursion-depth limit (`limits.task_max_depth`, default 2). A failed child
  returns `ERROR ...` without sinking its siblings. Each prompt directory also
  becomes a direct agent tool (`autocoder`, `commit` by default). Children load
  their own prompt and select their own tools — they never inherit the parent's
  tool surface, read set, undo snapshots, todos, or search cache.
- **Wiki mode.** Maintain local Obsidian-style vaults through deterministic
  `wiki_*` tools and `js --wiki=<modes> --vault=<v>`. `ingest` writes one source
  page per raw unit (plus candidate entity/concept lists); `synthesize` owns the
  shared entity/concept and cross-source synthesis pages; `query` answers from
  the vault; `lint` checks vault health. The two write modes are enforced at the
  tool layer so a cheap model in a wide fleet physically cannot write a shared
  page during ingest, making parallel ingest fleets disjoint and safe. Includes
  single-file ingest (`js --wiki=ingest --vault=<v> <FILE>`), orphan self-heal,
  file conversion (PDF/office/HTML/image/audio via
  pdftotext/pandoc/soffice/tesseract/ffprobe), qmd-backed search, and a
  vault-level lock that serializes only the shared log/commit moments.
- **`js-drain`.** Drain a wiki inbox or any folder (`-f DIR`) into a vault as
  sequential `js --wiki=ingest` jobs sized to the active model's context window:
  small files are packed into bundle jobs, large text is split line-aligned, and
  binaries pass whole. Originals are left in place by default and only moved to
  `Clippings/` with `-a` after every job covering them succeeds. Adds Ralph mode
  (`-R`, whole-file jobs), a `-l N` job limit for smoke tests, a live in-place
  TUI on a TTY (line-per-event when piped), and an end-of-run failed/interrupted
  summary with a copy-paste retry command per failure. A clean drain exits `0`;
  any failure exits `1`.
- **Artifact mode.** Curate and query a local artifact library through
  `artifact_*` tools and `js --artifact=<modes>`: `curate`, `digest`, `query`,
  and `lint`, organized over stable broad topic shelves and driven through the
  external `artifact` CLI.
- **Inline prompt directives.** The assembled system prompt expands `{{VAR}}`,
  `!{env NAME}`, and `!{file PATH}` (all always on), plus `!{sh ...}`/
  `!{python ...}`/`!{node ...}`/`!{c ...}` and fenced ` ```!lang ` blocks that
  run a subsystem and inject its stdout. Code-running directives are gated behind
  `--dangerously-evaluate-inline-code`. Expansion is single-pass and
  injection-safe (subsystem output is never re-scanned).
- **Vision support.** Robust image/PDF detection driven by a model-name
  heuristic (overridable with `JS_VISION`); when enabled, `read` sends image
  bytes for that one turn while session history keeps only a text stub, so a
  base64 image is billed once instead of resent on every following turn.
- **Config is a `set`-script now, not TOML.** The whole config system is one
  mechanism: `set <key> <value>`. Every knob is defined once in a registry in
  `js/settings.py` — its key, type, default, env var, and how it shows when
  empty. That one registry builds the env layer, the first-run file, the
  `set`/`show` commands, and the docs, so they can't fall out of sync. The
  config file is a plain list of `set` lines read at startup: global
  `~/.config/js/jsrc`, project `.js/jsrc`, local `.js/jsrc.local` (low to high,
  then env vars, then `--extra`). TOML is gone — deleted `_KNOWN_KEYS`,
  `_ENV_OVERRIDES`, the TOML loader, and `docs/config.example.toml`.
  `js --migrate-config` rewrites an old `config.toml` as a `jsrc` once, then you
  delete the old file; that flag goes away in 2 releases. The same `set` works
  live in the REPL (`/set`), and `/show` lists every knob and its current value.
  Empty states mean what they say: `off` = boolean off, `<none>` = nothing set,
  `<unset>` = not sent so the provider default wins; `provider.api_key` shows
  `<set>`, never the key. Registered the knobs the old list never had but the
  code already reads — `compact.auto`, `compact.model`,
  `compact.summary_max_tokens`, `subagents.prefer_inherit`,
  `subagents.lock_model`, `tools.alias_profiles`. Fixed `/set debug`, which used
  to silently flip `trace` instead.
- **Agent config out of markdown, into `00-tools.yaml`.** An agent's tools,
  model, and sampling live in a plain YAML `00-tools.yaml` now — config only, no
  prompt text. The prompt stays in `01-prompt.md`. An old `00-tools.md` with
  `---` frontmatter still loads for 2 releases and prints a one-time deprecation
  note.
- **Append-only compaction.** `/compact [focus]`, `/compact up to here`, and
  offline `js --compact <session>` append a compaction mark instead of rewriting
  the JSONL; on load the mark rebuilds context as the system prompt, one
  `<compaction-summary>` user message, and a verbatim recent tail whose boundary
  backs up so an assistant `tool_calls` message is never split from its results.
  Post-turn auto-compaction triggers on context fullness, with stream usage
  capture and cache-token normalization for DeepSeek and OpenAI usage shapes. An
  optional `set compact.pre_hook` can steer the summary.
- **Layered agent prompts.** Prompt directories are discovered from repo
  `prompts/`, global `agents/` in the platform config dir, and project
  `.js/agents/`, with project scope winning over global winning over repo.
  Global then project `AGENTS.md`/`AGENTS.local.md` are prepended to every main
  and subagent prompt. Bundled prompt dirs: `defaultagent`, `autocoder`,
  `commit`.
- **Provider login flow.** `js --login` opens a registry picker of saved,
  env-configured, and known providers; `js --login <provider>` validates the
  credential by listing models and saves it to `~/.config/js/logins.toml` with
  the model list cached in `~/.config/js/models-cache.json`. Multiple providers
  can be logged in at once; `js --logout <provider>` removes a login and its
  cache. A `<add custom provider>` option saves arbitrary providers backed by an
  `openai-completions`/`openai-responses`/`anthropic` API shape.
- **REPL model picker.** `/model` and `/pick-model` open a Textual picker
  showing saved logins and their cached models. Quick in-session switching is
  available via `/provider`, `/baseurl`, `/apikey`, `/login`, `/logout`,
  `/models`, and `/model model/id`.
- **First-class local and custom provider shortcuts** so they appear in
  `js --login`, REPL `/provider`, and the picker without configuring a custom
  provider first: local `llama.cpp` (`http://127.0.0.1:8080/v1`), Ollama
  (`http://127.0.0.1:11434/v1`), and Xiaomi MiMo API and Token-Plan endpoints.
- **OpenAI Codex OAuth provider.** `js --login openai-codex` runs the browser
  PKCE flow and `js --login openai-codex-device` runs device-code auth; the
  runtime talks to the Codex Responses endpoint with refreshable ChatGPT OAuth
  tokens kept only in the private login store.
- **models.dev model catalog.** js keeps a writable local mirror of the
  models.dev catalog under platform data, uses it to size max-output and
  compaction context windows, auto-refreshes when it is older than 72 hours, and
  exposes `js --refresh-model-catalog` / REPL `/refresh-model-catalog` to force
  it.
- **`JS_REASONING` env override** for `model.reasoning_effort`, matching
  `--reasoning` and REPL `/set reasoning`.
- **`-q`/`--quiet` flag** to suppress the "Continue: ..." resume hint after
  one-shot prompt mode.
- **Richer `-d` debug trace and `--debug-file PATH`.** Debug runs show a run
  header (model, endpoint, output cap, effort, vision, tool count), per-call
  timing, and the full system prompt / tool schemas / messages sent each call;
  `--debug-file` sends that trace to a file while keeping the clean answer on
  stdout. Wiki tools also print concise `[wiki]` progress to stderr during
  `--wiki` runs.
- **JSON bridge commands** for external pickers: `--providers-json`,
  `--logins-json`, and `--models-json <provider>`, each printing a single line.
- **Documentation.** README, full harness docs (wiki, drain, artifact,
  subagents, tool system, configuration/sessions, inline directives, models and
  providers, technical and user guides), an annotated `config.example.toml`, and
  agent-skill docs (issue tracker, triage labels, domain).
- **CLAUDE.md "quick do shit" agent mode** — a hard cap on ceremony when the
  operator signals urgency — and commit-policy guidance teaching the commit
  agent to write operator-readable changelog entries (lead with what broke, then
  what now works) instead of restating the diff.

- **Deterministic commit helper (`js.commit_helper`).** Two subcommands run from inside a repo: `survey` produces one compact snapshot (branch, porcelain status, every diff hunk numbered per file, untracked files, recent log) so the commit agent reads once instead of probing; `stage <file> <hunks|all>` stages exactly the named hunks via `git apply --cached --recount` (or `git add` for new files), replacing the fragile interactive `git add -p` dance with deterministic git plumbing.
- **`--preset NAME[,NAME...]` flag.** Layer `jsrc.<name>` files on top of base config in order (last wins); project `.js/jsrc.<name>` overrides global `jsrc.<name>`. Repeatable and comma-list, so `--preset fast,debug` stacks both.
- **`/set -key` knob clearing.** `/set -sampling.temperature` (with a leading dash) clears the knob back to its default instead of lingering at the last live-set value; works for any registered or dotted knob.
- **`runtime.allow_inline_code` config knob.** Inline-code eval (`!{sh|python|...}`) is now a real jsrc/env knob (`JS_ALLOW_INLINE_CODE`) instead of only the `--dangerously-evaluate-inline-code` CLI flag.
- **Canonical `JS_<DOTTED>` env vars for every knob.** `JS_SAMPLING_TOP_P`, `JS_LIMITS_MAX_READ_LINES` etc. work alongside any hand-picked short alias; the short alias wins when both are set.
- **`just install` / `just uninstall` recipes.** `just install` puts `js` + `js-drain` on PATH via `uv tool install --editable` so they track the working tree; `just uninstall` removes them.
- **Model picker persistence.** `/model` and `/pick-model` now write the selected model as `set model.id provider/model` in the global jsrc, so the choice survives restarts and boot-time config picks it up.
- **Subagent model control with sampling frontmatter and conditional tool descriptions.** `prefer_inherit` and `lock_subagent_model` config knobs control whether subagents inherit the parent model or use their own frontmatter primary. Agent `00-tools.yaml` carries `sampling:` (temperature, top_p, top_k, repetition_penalty, presence_penalty) as a typed object, plus `model:` and `secondary_model:` (a marked no-op stub for a future non-config flag). Tool descriptions support `<!--if:FLAG-->...<!--endif-->` conditional blocks so the task tool's `model` section and schema both vanish when subagent model override is locked off. Subagent model precedence: tool-call `model` arg (unless locked) → inherit parent (if `prefer_inherit`) → frontmatter primary → parent model fallback.
- **`--bench AGENT` mode.** Runs each `NN-benchmark.md` turn on a clean slate (fresh context, no session), measuring TTFT, tok/s, and wall-clock turn time. The persona is rebuilt per benchmark; benchmarks never see each other. Max tokens resolve per turn: `--max-out` > per-benchmark frontmatter > agent `00-tools.yaml` default.
- **`--stats-json PATH` and `--stats-csv PATH`.** Write per-turn timing and token statistics (TTFT, tok/s, turn time, output/prompt/cached tokens) for both normal one-shot runs and `--bench` mode, using the same aggregation code path.
- **Agent-default `max_tokens` cap from `00-tools.yaml`.** The agent manifest's `max_tokens:` field becomes the per-call output cap for one-shot, REPL, and bench runs. Config, `JS_MAX_OUTPUT_TOKENS`, and `--max-out` all override it; `-1` means uncapped (fall back to the provider/metadata default).
- **REPL Ctrl-C keeps partial work when the turn produced output.** When an interrupt lands after the model emitted assistant or tool messages, the partial turn is persisted (with orphaned tool calls healed in memory) and survives a session reload. A bare user prompt with no model output is still dropped cleanly.

### Changed

- **`show <key>` output includes the setting's doc string.** The single-key variant now prints the setting's documentation on a second line below the value, so users can see what a knob does without leaving the REPL.
- **`just install` now catches stale or shadowed launchers.** After reinstalling, the recipe verifies that the `js` command on `PATH` imports this working tree so users do not keep running an old or foreign install by mistake.
- **Inline-code documentation matches default-on execution.** Operator docs and repo guidance now describe `runtime.allow_inline_code` as enabled by default, `--im-a-pussy` / `JS_ALLOW_INLINE_CODE=0` as opt-outs, literal directive escaping, and non-fatal expansion failures so users understand the current prompt-security model.
- **Claude local configuration can be tracked deliberately.** The root `.claude/` directory is no longer ignored, so project-specific Claude settings and guidance can be reviewed and committed when they are real repo work.
- **Commit-agent subject guidance.** Claude Code repo instructions now require plain-English commit subjects so automated commits keep the owner's voice instead of leaking conventional-commit syntax.
- **Non-blocking window design plan.** Updated the design notes to reflect the completed async runtime and shared-loop supervisor, replacing the old worker-thread build path with the next output-event wiring step.
- **Subagent fan-out scheduling.** Subagent tasks now run as async turns gathered on the active REPL supervisor when present, keeping child jobs visible and cancelable while preserving ordered results outside the REPL.
- **Runtime turns can run without blocking the shared event loop.** `run_turn_async` now awaits async model streaming, uses non-blocking retry sleeps, and dispatches synchronous tools through an executor; the existing `run_turn` remains a sync wrapper for current CLI and subagent callers while tests patch the async model seam.
- **Model streaming exposes an async primitive.** `stream_model_async` now runs on the caller's event loop and closes the provider asynchronously, while `stream_model` remains as the blocking compatibility wrapper for existing sync callers.
- **Async model streaming concurrency is covered by tests.** A new stream test proves two model turns can overlap on one shared event loop instead of serializing through per-call `asyncio.run` loops.
- **Commit workflow recipe simplified.** `just commit` is now a no-argument wrapper around `js --commit`, preventing target paths, prompts, or prewritten messages from bypassing the commit agent's own inspection, staging, and message choices.
- **Ignore rules narrowed to local scratch files.** Repo-root TODO/FIXME/review/handoff patterns remain ignored, while `CLAUDE.md`, `AGENTS.md`, and project `.js/` content are no longer globally hidden so real guidance and project config can be tracked deliberately.
- **Dependency `ai` unpinned from `==0.2.0` to latest compatible.** `pyproject.toml` drops the version pin; `uv.lock` upgraded to `ai==0.2.1` (plus all transitive deps — anthropic, openai, pydantic-core, pytest, ruff, jiter, certifi, anyio, tqdm, wcwidth). Dropped transitive deps from the lockfile that `ai` no longer pulls: `mcp`, `cryptography`, `cffi`, `pycparser`, `pyjwt`, `click`, `uvicorn`, `starlette`, `sse-starlette`, `httpx-sse`, `python-multipart`, `python-dotenv`, `pydantic-settings`, `jsonschema`, `jsonschema-specifications`, `referencing`, `attrs`, `rpds-py`, `pywin32`.
- **Tool schema ported from `FunctionToolArgs`/`args` to `ToolSpec`/`spec`.** ai>=0.2.1 renamed the tool argument class and field; `js/codex_provider.py`, `js/model_client.py`, `tests/test_codex_oauth.py`, `tests/test_model_client.py` updated accordingly.
- **`ai.Model` constructor ported to keyword-only `id=` parameter.** All callsites (`js/model_client.py`, `tests/test_codex_oauth.py`, `tests/test_model_client.py`) use `ai.Model(id=..., provider=...)` instead of the old positional `ai.Model(..., provider=...)`.
- **Stream params ported from flat dict to structured `InferenceRequestParams`.** The old `params: dict[str, Any]` plumbing is replaced by `ai_params.InferenceRequestParams` with typed sub-objects (`OutputParams` for `max_tokens`, `ReasoningParams` for `reasoning_effort`, `SamplerParamsMap` for sampling knobs). `js/model_client.py` gains `_sampler_map` and `_build_inference_params` helpers that encode the existing transport-aware sampling filter (anthropic drops penalties, openai takes temp/top_p/presence_penalty, openai-compatible adds top_k/repetition_penalty) into the structured model. DeepSeek `max_reasoning_tokens` now rides `extra_body` unconditionally (the old code had separate explicit-provider vs. gateway paths that are no longer needed). Test assertions in `tests/test_model_client.py`, `tests/test_provider_params.py`, `tests/test_login_compact_cmds.py`, `tests/test_repl_harness.py`, and `tests/test_codex_oauth.py` updated to use a `_pview` helper that flattens the structured params back to dicts for readable assertions.
- **OpenAI chat-completions wire protocol pinned for openai-SDK providers.** ai>=0.2.1 flipped the OpenAI SDK's default wire from chat-completions to the Responses API; every endpoint `js` targets through the openai SDK (opencode-go, mimo, ollama, llama.cpp, custom OpenAI-compatible base URLs) 404s on `/responses`. `resolve_model` now passes `OpenAIChatCompletionsProtocol()` to `ai.get_provider()` for `sdk_provider_id == "openai"` unless the transport is `"custom_responses"`.
- **Models.dev catalog refresh TTL lowered from 72h to 8h.** The `_CATALOG_MAX_AGE` constant in `js/model_metadata.py` drops to 8 hours so model metadata (new models, window sizes) is picked up more promptly.
- **OpenAICodexProvider ported to the ai>=0.2.1 frozen-pydantic Provider base.** The provider is a pydantic model now: `provider_class_id`/`name`/`default_base_url` are class fields, the upstream httpx client installs via `_set_client`, and the rotating OAuth state (access/refresh token, account id) lives in `PrivateAttr`s so token refresh still mutates in place. `_build_body_async` reads the structured `InferenceRequestParams` (effort from `ReasoningParams`, temp/top_p from the sampler map) instead of a flat dict. The four quarantined tests are un-skipped and pass, plus an end-to-end test that drives the whole `stream_model` → `ai.stream` → provider chain over a fake Codex wire.

- **`--list-models` covers every saved login, not just the active provider.** With no argument the listing spans all saved logins (cache-first, offline-friendly); a named provider still scopes to that one. Output is one `provider/model` per line with no extra prose.
- **Client-side model allowlist gate removed.** `resolve_model` no longer rejects models absent from a provider's static `allowed_models` tuple — that list is a curated hint for filtering noisy `/models` listings, not an authority on what the endpoint serves. The provider is the source of truth and will 400 on genuinely unknown ids itself.
- **Saved-login model-id prefix overrides a pinned provider.** A model id like `opencode-go/glm-5.2` that names a provider the operator has logged into now routes through that provider even when a different `provider.id` is pinned in jsrc, carrying the saved login's credentials. This does not hijack gateway ids like `anthropic/claude-...` under a pinned `omp` when there is no saved `anthropic` login.
- **Login secondary test uses `1+1=` and enter always skips.** The verify prompt is a trivial arithmetic question instead of a factoid. Enter always means "add without testing" regardless of `require_test` mode; a model number still tests that specific model.
- **`allow_inline_code` reads from config settings, not env-only.** The knob is settable via `set runtime.allow_inline_code on` in jsrc or the `JS_ALLOW_INLINE_CODE` env var (which the `--dangerously-evaluate-inline-code` flag already sets).
- **Sampling env reads accept `JS_SAMPLING_<FIELD>` canonical names.** `JS_SAMPLING_TEMPERATURE`, `JS_SAMPLING_TOP_P`, etc. work alongside the short aliases (`JS_TEMP`, `JS_TOPP`); the short alias wins when both are set.
- **Docs reconciled with shipped behavior.** `fetch` documented as the full HTTP client it is (method/headers/body/json_body/save/download, `file://`, 32 MiB cap); `remove` documented with trash-by-default, the `permanent` flag, and the 512 MiB guard; the `-C`, `-q`/`--quiet`, `--ignore-local`, `--ignore-global` flags, the `commit_helper` CLI, and slashless `set`/`show`; plus the `minimax`, `omp`, `cliproxyapi`, `ollama-cloud` providers and the `DEEPSEEK_API_KEY` auto-select (`reasoning_effort=xhigh`).
- **Artifact config actually applies.** `set artifact.dir/url/bin` in jsrc now reaches the artifact tools. Precedence: jsrc config → `ARTIFACT_*` env → built-in default (`/srv/artifacts`, `http://localhost`, `artifact`). `Config` carries the fields and `run_turn` copies them onto the tool context — before, they sat unread in the settings dict and the tools always used the env/default.
- **Wiki vaults fail closed.** No more silent `creative` default. Give a vault with `--vault <alias|path>`, or run inside one (a `PURPOSE.md` sentinel or a `wiki-*` directory, walking up); otherwise the run stops with a clear error. Aliases are config now (`set wiki.aliases.creative /path`) — the hard-coded `creative`/`general` are out of the code and shipped in the stock jsrc instead. `resolve_vault` reads aliases off the tool context; `infer_vault` returns nothing when it can't find a vault.
- **Source + tests modernized by ruff safe autofixes.** Dequoted forward-ref annotations, `lru_cache(maxsize=None)`→`cache`, and deprecated-import updates; `js/toolkit/wiki/prompts.py` is excluded as a prompt-template builder.
- **Replaced LiteLLM with the Vercel AI Python SDK (`ai-python`).** The provider
  boundary now lives in `js/model_client.py`; `js/runtime.py` no longer imports
  `litellm` or any LiteLLM-shaped chunk/exception helpers. The `litellm_proxy`
  pytest marker was retired and an `ai_provider` marker added for live-provider
  tests. `requires-python` was bumped to `>=3.12` and the dependency pinned to
  `ai[openai,anthropic]==0.2.0`.
- **No built-in proxy route.** Default model is `deepseek/deepseek-v4-flash`;
  unprefixed model ids route through AI Gateway and `provider:model` ids go
  directly to the named provider. Explicit `set provider.id/base_url/api_key` is
  opt-in; otherwise official SDK env vars (`AI_GATEWAY_API_KEY`,
  `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`) are read directly by
  the SDK. `JS_MODEL` overrides the configured model and `ME_MODEL` is a silent
  alias that applies only when `JS_MODEL` is unset.
- **Config and tool inputs are sanitized.** Numeric config keys reject boolean
  values, read/search line and byte bounds ignore boolean and negative inputs,
  and shell, artifact, wiki, and semantic-search parameters go through shared
  sanitizers — so a model passing a mistyped or out-of-range argument no longer
  poisons a run.
- **Wiki/artifact in-process mode is threaded through `ToolContext`** instead of
  mutating `os.environ`, and runtime limits are exposed as `limits.*` knobs (fetch
  timeout defaults to 15s, wiki vault locks to 30s).
- **Provider reasoning parameters use provider-accepted fields.** DeepSeek gets
  `max_reasoning_tokens=32000` when reasoning is enabled, sent via `extra_body`
  on direct OpenAI-compatible transports rather than as an invalid top-level
  kwarg; MiniMax strips the OpenAI-shaped reasoning object its adapter rejects;
  Codex keeps reasoning as a separate request knob instead of a suffixed model
  id; and reasoning content is round-tripped only on assistant turns that carry
  tool calls, where providers require it, saving wasted prompt tokens elsewhere.
- **opencode-go Zen "go" plan is split into two transports** — `opencode-go`
  (OpenAI-compatible) and `opencode-go-anthropic` (Anthropic-compatible) — each
  sharing one API key but filtering its model list to the set its transport
  actually serves, so selecting an unsupported model is rejected before a request
  is sent.
- **Claude tool-name handling is opt-in.** The runtime no longer auto-renames
  `read`/`write`/`task` to `Read`/`Write`/`Task` whenever the model id contains
  "claude"; the rename is now configured via `set tools.alias_profiles`, which
  matches model/provider substrings and rewrites schema names (and backtick
  cross-references) outbound while dispatching the alias back to the canonical
  handler. No profiles ship by default, so default tool names are unchanged.
- **Default model** updated to `deepseek/deepseek-v4-flash` (from the earlier
  `deepseek-v4-pro`/proxy default).
- **`read` caps `.jsonl` lines separately** (`limits.jsonl_max_line_chars`, env
  `JS_JSONL_MAX_LINE_CHARS`, default 65536) so single-line JSON records aren't
  truncated, while other files keep `limits.max_line_chars`.
- **Tool descriptions moved** to `js/toolkit/tool_descriptions/` alongside the
  tool implementations, and the autocoder agent prompts were reorganized for
  consistent numbering with a tools definition added.
- **`banks/` and `handoff/` are git-ignored.** These directories hold per-session
  runtime state (SQLite DBs, handoff notes, prompt audits) that were never meant
  to be tracked; accidental commits of local workspace data are now blocked.

- **Default agent toolbelt: `wiki_*` out, `artifact_*` in.** The default agent doesn't carry `wiki_*` — wiki work goes through `--wiki` or a dedicated wiki agent. `artifact_*` is on the default toolbelt.
- **Default agent prompt rewritten as the SHE APE persona.** `prompts/defaultagent/01-prompt.md` went from a 40-line generic blurb to KING's full persona (244 lines): conversation-first, a body/emotion barometer, telegraphic voice, separate code/sysop/assistant modes, a writing/character mode for KING's adult-fiction work, and headless + `{reasoning}` scaffolds. It names tools by what they do instead of hardcoding a list and carries no "what you can't do" prose — so the prompt can't claim a tool the agent doesn't have.
- **`remove` trashes by default and won't follow symlinks.** Targets up to 512 MiB go to `trash`/`trash-put` (undo snapshot taken first); over 512 MiB it stops instead of stuffing trash; `permanent=true` deletes directly. Paths resolve without following symlinks, so a symlink is unlinked as itself, never its target. No `trash` binary → it says so and points at `permanent=true`.
- **Commit bot is wired up.** `js --commit` drives the deterministic `js.commit_helper` (survey + `stage`) instead of `git add -p`: it injects a one-shot survey snapshot into the kickoff, auto-runs `git init` in a non-repo, binds the helper to the target dir with `-C`, and never reports a git failure as a clean tree. The repo commit prompt is generic; a personal push/remote stays a `~/.config/js/agents/commit` override.
- **Model routing lives in one resolver.** `js/routing.py`'s `resolve_model_route` turns a requested model id plus provider hints into a full route (model, provider, base URL, key, headers, transport). `from_env`, the REPL active-model switch, and compaction all go through it instead of three copies of the prefix-parsing logic; `model_client.resolve_model` is now just the final `ai` provider factory.
- **Sampling is typed and per-wire — no more `os.environ` mutation.** A frozen `Sampling` object flows through the call instead of being written to `JS_*` env vars. Each set field is sent per the provider's wire: anthropic takes temperature/top_p/top_k and drops the OpenAI/vLLM penalties, OpenAI takes temperature/top_p/presence_penalty, OpenAI-compatible adds top_k/repetition_penalty via `extra_body`, and an unknown wire sends nothing. Precedence low→high: `set sampling.*` (jsrc) < agent `00-tools.yaml` `sampling:` < `JS_*` env < CLI/live. Loading an agent no longer leaks sampling into the process env.
- **Agent model selection runs through the resolver.** `--agent <name>` applies that agent's `00-tools.yaml` `model:` (a provider-prefixed id re-routes the provider too), unless you pin one with `-m` / `JS_MODEL`. A subagent's frontmatter `model:` likewise re-routes its provider, not just the model string. `lock_subagent_model` drops the task tool's `model` arg entirely; frontmatter `model:` survives the AGENTS-file prepend.
- **`fetch` is whole-hog.** The `fetch` tool grew from GET-only text to methods + headers + raw/JSON body, `file://` URLs, download-to-disk (`save`, 32 MiB cap), binary descriptors instead of mojibake, and image-for-vision (an `image/*` response becomes a vision part when the model supports it). Errors still come back as `ERROR:` strings, never raises into the loop.
- **Prompt file attachments (`-f`/`@file`).** `js -p "..." -f path` attaches files to a one-shot prompt (repeatable; `-f -` reads stdin bytes); the REPL takes an `@path` token. Text inlines (delimited, ≤64 KiB); images attach as vision FileParts when the model supports vision, else a "vision off" note; other binaries become a short descriptor. `history_to_ai_messages` now routes non-string user content through `_coerce_parts` so image parts reach the model while persisted history keeps a text stub.
- **Provider audit pass.** DeepSeek's cache-hit rate now shows in the `-d` trace (`cache NN%`, from the SDK's normalized cache-read tokens). Local OpenAI-compatible / Ollama / llama.cpp servers are probed for their real context window (vLLM `/v1/models`, Ollama `/api/show`, llama.cpp `/props`) and that wins over name-based guessing for compaction math — best-effort, falls back cleanly on any failure, no new model-name heuristics. New offline evidence tests assert the exact params emitted per provider/wire (DeepSeek `max_reasoning_tokens` via `extra_body`, anthropic drops penalties, OpenAI gets `reasoning_effort`, append-only history not rewritten).
- **De-slop pass.** `ruff check` is clean — cleared the standing lint debt (`codex_auth`/`codex_provider` `Login` F821s via `TYPE_CHECKING` imports, a dead `meta` local, re-export F401s). Removed prose-policing/ceremony doc tests that only froze exact wording or banned removed substrings (kept every test that exercises real behavior); fixed a few stale doc lines. `test_docs_guidance` is intentionally much smaller now.
- **`ModelStreamResult` carries per-call stream timing.** `first_token_s` (time-to-first-text-token, `None` when the model emitted no visible text) and `elapsed_s` (wall-clock duration of `ai.stream` itself) are measured inside `_stream_async`, isolated from `run_turn`'s setup and bookkeeping, for honest TTFT and tok/s reporting.
- **`run_turn` debug trace now includes TTFT.** The `-d`/`--debug` per-call finish line prints `ttft Nms` alongside the existing elapsed ms, tok/s, and cache-hit percentage.
- **Prompt expansion subprocess timeout raised from 30s to 120s.** Long-running `!{sh ...}` and ` ```!lang ` blocks in system prompts no longer time out during agent startup.

### Removed

- **Client-side model allow-list.** The hardcoded `_OPENCODE_GO_OPENAI_MODELS`/`_OPENCODE_GO_ANTHROPIC_MODELS` tuples, the `ProviderDef.allowed_models` field, `filter_models`/`supports_model`, and the unused `filter_login_models`/`provider_supports_model` helpers are gone. `logins.fetch_models` returns the endpoint's live `list_models` verbatim, so `js --login` and the `/model` picker show exactly what the provider serves (e.g. opencode-go's `glm-5.2`, previously hidden by the stale tuple). The endpoint stays the one authority — an unknown id 400s with the provider's own message instead of being pre-filtered.
- **Stale docs/comment.** The picker `:` command-mode vocabulary (`:/provider`, `:/key`, … — never implemented; `picker.py` has no `:` binding) is cut from the user guide, and the `JS_MODEL`/`ME_MODEL` troll line in `config.py`'s `from_env` docstring is gone (it was a one-off troll, done).
- **`ME_MODEL` env alias.** The silent env-layer alias for `model.id` (applied when `JS_MODEL` was unset) is gone for real — `config.py` explicit-model check, `settings.py` table entry + special-case, and every doc/test reference. A prior pass had renamed it 'silent' instead of deleting; this removes it. Model override is `JS_MODEL` / `-m` only.
- **LiteLLM.** The `litellm` dependency, the `litellm_proxy` pytest marker, and
  the regenerated `uv.lock` no longer carry LiteLLM or any of its transitive
  dependencies.
- **Legacy proxy and aliasing config.** Removed `provider.api_base`,
  `OPENAI_API_BASE`, and the implicit "model id contains claude" tool-alias
  magic. Use explicit `set provider.id/base_url/api_key`, official SDK env vars,
  or opt-in `set tools.alias_profiles`.
- **Transitional `fs_*` tool names** retired for the canonical lowercase surface
  during the tool-definitions rework. (Earlier Forge-era aliases such as
  `read_file`/`write_file`/`grep`/`bash` were also dropped.)
- **Deprecated agent prompts** — cockmongler (replaced by, then itself replacing,
  the curator), curator, ircii, wikiape, ape-creativeingest, and the old
  top-level ape prompt — along with the `remove` tool from the commit agent's
  surface.
- **Stale scratch and spec files.** Three bash launcher scripts (`1`, `2`, `3`)
  for long-deleted TUI picker examples, the pre-existing test-failure tracker
  (`TODO.md`), a 32 MB OMP session HTML export, OMP provider-rebuild plan specs,
  and the old LiteLLM-era `uv.lock` are gone from tracking; `.gitignore` was
  reorganized by category with these patterns added.

### Fixed

- **Byte-limited UTF-8 reads no longer misclassify valid text as binary.** Attachment detection drops only an incomplete trailing multibyte sequence, and artifact previews decode split characters leniently so non-ASCII content still shows useful text.
- **Non-blocking REPL state resets no longer race active turns.** `/reset`, `/wipe`, and `/compact` are refused while a turn is running so they cannot clear the live message list mid-append and corrupt output.
- **Drain staging no longer overwrites split files that share a stem.** Large text pieces include a per-source sequence prefix, so same-named files in different subdirectories both survive staging.
- **Rollback and compaction markers no longer cut the wrong messages after healing orphaned tool calls.** Session loading now heals before applying marker indexes because those indexes were computed against the healed live message list.
- **OpenAI-compatible providers no longer receive unsupported structured `top_k` or `repetition_penalty` params.** Those knobs stay in raw `extra_body` and merge with provider extras while supported sampler fields remain structured.
- **Undo no longer misses remove snapshots through symlinked parents.** It checks both resolved and no-follow snapshot keys so deleted files can be restored regardless of how the snapshot was keyed.
- **Wiki conversion no longer returns stale office output or treats binary files as text because their path contains `text`.** LibreOffice results are accepted only after a successful fresh conversion, and `file` fallback classification inspects the description rather than the path.
- **Non-blocking REPL no longer drops a submitted turn on EOF.** Graceful quit now waits for queued and in-flight turns to finish before teardown, so headless or piped prompt sessions persist completed work instead of cancelling it during shutdown.
- **Reasoning effort is a dial snapped to each model's real stops.** js exposes one seven-stop knob (`none<minimal<low<medium<high<xhigh<max`), but no endpoint serves all of them — Xiaomi MiMo 400s on anything outside `low|medium|high`, kimi caps at `high`, glm and DeepSeek take `xhigh`/`max`. `js/reasoning.py` snaps the requested stop to the nearest one the target serves (ground-truthed by live probe, not vendor docs), so `-r xhigh` on MiMo sends `high` instead of erroring. Fixes the MiMo `reasoning_effort` 400 (it was never a MiniMax — MiMo is Xiaomi's model and was never gated).
- **Sessions resume and switch models cleanly across the OpenAI wire.** ai's chat-completions protocol re-serializes a replayed assistant turn's chain-of-thought as a non-standard `message.reasoning` field that the glm backend rejects ("Extra inputs are not permitted, field: messages[..].reasoning"), so resuming a tool-using session onto glm — or switching to it mid-conversation — 400'd on every prior turn. `stream_model` now strips replayed reasoning for backends that reject it (glm), while backends that accept it (mimo, kimi) and providers that require it (DeepSeek's own `reasoning_content`) keep theirs untouched.
- **`--model` provider prefix now routes correctly in interactive mode.** The REPL path calls `_resolve_cli_model_override` so a provider-prefixed model id on `-m` re-routes provider, base URL, and API key instead of passing the raw string as a model override the backend can't resolve.
- **Session resume hint echoes `--agent` for non-default agents.** Plain `-p` mode has no `resume_prefix`, so the "Continue:" hint must include `--agent <name>` when a non-default agent is in use; without it `js --session ...` resolves against `sessions/defaultagent` and 404s the session JSONL.
- **Inline-code interpreters run in the invocation cwd, not the temp compile dir.** Interpreted (`!{python}`, `!{node}`, etc.) and compiled (`!{c}`) directives now inherit the shell working directory so env-probing snippets see the real project; the snippet temp file is never left behind in the working directory.
- **REPL event handler errors now appear in debug telemetry.** Direct `state["events"].emit()` calls in the REPL (for `input` and `cancel` events) were replaced with `_emit_repl_event()`, which logs handler failures to the debug log instead of silently swallowing them.
- **Event hooks close out error and cancel paths.** Fatal model-call errors now emit `turn_end` with an `error` reason after the `error` event, and Ctrl-C during a REPL turn emits `cancel` before rollback, so `/on` hooks can observe cleanup paths instead of only successful turns.
- **Past-EOF `read` ranges are no longer retriable errors.** Asking `read` for a start line beyond the end of a file now returns a non-error EOF note with the file's total line count instead of `ERROR: invalid line range`, avoiding pointless retry loops during agent inspections.
- **`/compact-auto` no longer misroutes into `/compact`.** REPL command dispatch matches `/compact` exactly (`== "/compact"` or a `"/compact "` prefix) instead of `startswith("/compact")`, so `/compact-auto on` reaches its own handler instead of firing a compaction with a garbage `"-auto on"` focus.
- **Tests no longer snap on operator state.** Prompt-dir agent tests discover `prompts/` dynamically (so agent churn can't break them), and the provider-shortcut test isolates saved logins + env so `/provider ollama` resolves DEFAULTS on any box instead of leaking the operator's saved ollama login.
- **Double-encoded tool-call args no longer flail the model.** `_canonical_tool_args` keeps the model's raw bytes only when they're already a JSON object; valid-but-double-encoded args (a JSON string wrapping the real object) get repaired to the canonical object, so the history resent each turn matches what actually executed instead of being blanked by the SDK's integrity pass.
- **`js --commit` and all tool use crashed.** `call_tool` was used in the
  runtime tool-dispatch path but never imported, so every tool call raised
  `NameError`, burned the retry limit, and aborted the turn. Importing it
  un-broke 9 tests that were failing only because dispatch couldn't run.
- **Tool-retry-limit and followup early-exits orphaned `tool_calls`.** A
  saturated error tracker (e.g. three parallel `fs_search` failures) could cut a
  tool batch after the first result, leaving `tool_calls` without their tool
  messages and getting the whole session rejected by DeepSeek. Now every tool
  call in an assistant batch gets its tool message appended before the turn
  returns, and the session loader backfills synthetic results for histories
  already corrupted this way so they replay again.
- **Provider picker routed wrong.** The picker now lists only saved logins and
  current manual targets instead of every SDK provider, keeps logout-cleared
  state cleared, invalidates stale model caches when credentials change, fixed
  routing leaks between providers, and sends vision images as a provider-visible
  `FilePart`.
- **OpenAI Codex model discovery** keeps `gpt-5.5` visible even when the
  model-list endpoint lags behind the live Responses route.
- **CLI `-m` flag now routes through the model resolver.** A provider-prefixed model id (e.g. `openai-codex/gpt-5.5`) on `-m` re-routes provider, base URL, and API key instead of passing the raw string as a model override that the backend can't resolve.
- **Wiki destructive re-archive.** Orphan detection no longer flags inbox names
  already safe in `Clippings/`, which had been re-archiving and eating
  freshly-staged logs; genuine-orphan self-heal still works.

- **Wiki vault detection no longer false-positives on bare `inbox/` directories.** `find_vault()` walked up to any directory containing an `inbox/` subdirectory, which matched home directories and any project with an inbox folder. Now only a `PURPOSE.md` sentinel or a `wiki-*` directory name marks a vault root.
- **Ctrl-C during an active turn that produced real work no longer silently discards the partial turn.** The REPL now keeps assistant and tool messages emitted before the interrupt, persists them with a `turn_interrupted` mark (instead of `rollback_to`), and heals orphaned tool calls in memory so the next turn starts from a valid state. A bare user prompt that produced nothing from the model is still dropped cleanly. The `cancel` event fires before the keep/abort decision so `/on` hooks always observe it.

### Security

- **Shell subprocess env is whitelisted.** The `shell` tool runs with a minimal
  whitelisted env (PATH, HOME, USER, SHELL, PWD, TERM, LANG, LC_ALL); secrets in the parent environment such as
  `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` do not propagate to model-run commands.
- **Login secrets are protected.** `logins.toml` is created with private
  permissions before any secret is written, and the `--logins-json` bridge nulls
  every login's refresh token and Codex `provider_api_key` so it never leaks long-lived
  credentials.
