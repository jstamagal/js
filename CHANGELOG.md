# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **REPL Tab completion.** Tab completes by prefix (never fuzzy) with a rotating menu: commands at line-start (a slashless word gets an implicit `/`, so `comp`→`/compact`), `/set`/`/show` knob keys, `/login`/`/provider` names, file paths for `/…`/`@…` tokens, and hunspell spelling suggestions for prose words. Tab-triggered only (`complete_while_typing=False`).
- **`/login <name> [apikey] [baseurl] [provider]`.** Build and persist a named login inline — a custom name with an explicit transport (e.g. a local vLLM endpoint as `openai`), or a known provider that infers its own type. Bare `/login <name>` still loads a saved login.
- **`/compact-auto on|off`.** Real handler that toggles `compact.auto` live (it was advertised in help with no handler before).
- **`/load <file>` ircII-style script loading.** Load slashless command scripts from the REPL or nested within other scripts. Script files accept `set`, `show`, `load` (nested, resolved relative to the containing script), and `on` — the same command surface as the REPL minus provider commands. Cycle detection and a depth limit guard against runaway nesting.
- **`/on [event handler]` typed event hooks.** Register and inspect named event hooks against the runtime event stream. Canonical events include `turn_start`, `turn_end`, `prompt`, `stream`, `response`, `tool_call`, `tool_result`, `error`, and more. The `^` prefix marks a hook as suppressive. The runtime now emits these events at the appropriate lifecycle points, so scripts can observe (and eventually react to) the full turn flow.
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
- **Subagent model control with sampling frontmatter and conditional tool descriptions.** `prefer_inherit` and `lock_subagent_model` config knobs control whether subagents inherit the parent model or use their own frontmatter primary. Agent `00-tools.yaml` carries `sampling:` (temperature, top_p, top_k, repetition_penalty, presence_penalty) as a typed object, plus `model:` and `secondary_model:` (a marked no-op stub for a future non-config flag). Tool descriptions support `<!--if:FLAG-->...<!--endif-->` conditional blocks so the task tool's `model` section and schema both vanish when subagent model override is locked off. Subagent model precedence: tool-call `model` arg (unless locked) → inherit parent (if `prefer_inherit`) → frontmatter primary → parent model fallback.

### Changed

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

### Removed

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
- **Wiki destructive re-archive.** Orphan detection no longer flags inbox names
  already safe in `Clippings/`, which had been re-archiving and eating
  freshly-staged logs; genuine-orphan self-heal still works.

- **Wiki vault detection no longer false-positives on bare `inbox/` directories.** `find_vault()` walked up to any directory containing an `inbox/` subdirectory, which matched home directories and any project with an inbox folder. Now only a `PURPOSE.md` sentinel or a `wiki-*` directory name marks a vault root.

### Security

- **Shell subprocess env is whitelisted.** The `shell` tool runs with a minimal
  whitelisted env (PATH, HOME, USER, SHELL, PWD, TERM, LANG, LC_ALL); secrets in the parent environment such as
  `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` do not propagate to model-run commands.
- **Login secrets are protected.** `logins.toml` is created with private
  permissions before any secret is written, and the `--logins-json` bridge nulls
  every login's refresh token and Codex `provider_api_key` so it never leaks long-lived
  credentials.
