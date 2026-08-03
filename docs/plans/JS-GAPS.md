# js Harness-Wide Completeness Audit

Read-only audit target: `js` at `/home/ronald_rump/js` (worktree state, branch `textual-repl-nonblocking`).
Reference agents: `~/Repos/agents/codex` (Rust), `~/Repos/agents/crush` (Go/Bubbletea), `~/Repos/agents/claude-code` (TS/Ink), `~/Repos/agents/opencode` (TS), and the pi substrate (`~/Repos/agents/little-coder/node_modules/@earendil-works/pi-coding-agent` + `~/Repos/agents/little-coder/.pi/extensions/`, with the oh-my-pi fork source for readable TS).

Compaction is NOT covered here — see `COMPACTION-GAPS.md` for that subsystem.

DRAFT SKELETON — reference columns being filled in.

## 1. Tool System

### What js does today

- Tools are frozen dataclasses with an OpenAI-shape schema and a sync handler taking a shared mutable `ToolContext` (`js/toolkit/core.py:24`, `js/toolkit/core.py:58`).
- Registry: fs (read/write/patch/multi_patch/fs_search/sem_search/remove/undo), process_net (shell/fetch), meta (todo_write/todo_read/followup/plan/skill/task), wiki, artifact, plus one auto-generated named-agent tool per prompt directory (`js/toolkit/registry.py:142`, `js/toolkit/registry.py:121`).
- Per-agent tool surface picked by `tools:` selectors with glob support and typo warnings (`js/toolkit/registry.py:87`).
- Model-facing alias profiles rewrite tool names per model match (`js/runtime.py:48`, `js/runtime.py:91`).
- Dispatch: `task` calls from one assistant batch run in a ThreadPoolExecutor; all other tools run sequentially in-order (`js/runtime.py:440`). Under the non-blocking supervisor, fan-out calls are awaited on-loop, leaf calls in one executor thread (`js/runtime.py:549`).
- Tool results are byte-capped with an explicit marker (`js/runtime.py:378`); shell output capped separately by `_run_capped` (`js/toolkit/process_net.py:56`); per-tool error counter aborts the turn at 3 consecutive errors per tool (`js/runtime.py:349`).
- Malformed tool-call args are repaired (`js/runtime.py:304`, `js/tool_args.py`).
- Safety rails: read-before-write hash tracking (`js/toolkit/core.py:96`), in-process undo snapshots (`js/toolkit/core.py:105`, `js/toolkit/fs.py:376`), remove-to-trash with 512MiB cap (`js/toolkit/fs.py:354`), shell env allowlist (`js/toolkit/process_net.py:26`).
- ABSENT: any permission/approval model, sandboxing, MCP client, background/PTY shell sessions, LSP integration, streaming tool output to the UI mid-execution, web search, notebook tools, image generation, plan-mode gating, tool-level timeout kill UI.

## 2. Session / History

### What js does today

- Append-only JSONL per agent per session at `sessions/<agent_id>/<session>.jsonl`, fcntl-locked, fsynced, version-tagged records (`js/memory.py:18`, `js/memory.py:200`).
- Control marks projected at load: `session_reset`, `rollback_to:N`, `compaction:{...}`, plus informational `turn_interrupted`/`turn_aborted`/`error:` (`js/memory.py:141`, `js/cli.py`, `js/tui.py:289`).
- Orphaned tool calls healed on load; orphan reasoning stripped for replay (`js/memory.py:68`, `js/memory.py:106`).
- Resume: `-s/--session` names an existing id/file (`js/cli.py:2834`, `js/config.py:187`); `latest.json` is written on every session reserve (`js/config.py:210`) but nothing reads it back — no `--continue`/`--resume-last`.
- `/reset` clears in-process (mark appended); `/wipe` rotates the file to `.bak` (`js/cli.py:1400`).
- ABSENT: session listing/picker, session titles/metadata, session tree/branching/fork, checkpoint/undo of file state tied to history position, replay, cross-session search, cost/token accounting per session, sharing/export.

## 3. Provider / Model Layer

### What js does today

- One provider boundary (`js/model_client.py`), Vercel AI Python SDK underneath; ~28 built-in ProviderDefs plus dynamic models.dev providers and saved-login synthesized providers (`js/providers.py:122`, `js/providers.py:483`, `js/providers.py:520`).
- Routing: `provider/model` prefix parsing gated on saved logins (env keys alone never route) (`js/routing.py:82`); borrowed-SDK endpoints refuse to fall back to vendor default endpoints (`js/providers.py:629`).
- Codex OAuth (ChatGPT) as a custom provider (`js/codex_auth.py`, `js/codex_provider.py`).
- Streaming with TTFT/elapsed measurement (`js/model_client.py:488`); usage incl. cache_read tokens recorded per call (`js/runtime.py:1045`).
- Retries: 3 attempts, exponential backoff + jitter, only `ai.ProviderAPIError.is_retryable` (`js/runtime.py:181`, `js/runtime.py:989`).
- Reasoning: one `-r` effort dial snapped per family (`js/reasoning.py`), DeepSeek budget via extra_body (`js/model_client.py:798`), reasoning replay stripped for backends that reject it (`js/model_client.py:251`).
- Model metadata: models.dev catalog mirrored locally with 8h refresh (`js/model_metadata.py:377`), live local-server context probes for ollama/llama.cpp/openai-compatible (`js/model_metadata.py:244`), cached per-login server limits (`js/model_metadata.py:522`).
- Per-model vision enablement by name heuristic + JS_VISION override (`js/config.py:84`).
- Sampling as typed object merged across setscript/env/CLI layers (`js/sampling.py`, `js/cli.py:597`).
- ABSENT: cost tracking/pricing (models.dev cost data unused), prompt-cache control (Anthropic cache_control breakpoints), Retry-After header handling, provider capability detection beyond vision-by-name (tool-call support, temperature support), fallback models, model aliases (latest -> pinned), parallel-tool-call capability flags, token counting endpoint use.

## 4. TUI / Output

### What js does today

- Three REPL surfaces: legacy blocking prompt_toolkit REPL (`js/cli.py:3100` region), `--nonblocking` asyncio REPL (`js/cli.py:2599`), and `--tui` Textual cockpit (`js/tui.py:93`).
- TUI: RichLog transcript with Markdown rendering of assistant turns, Input with history + tab completion, queueing while a turn runs, ctrl+c cancel/drain, /jobs + /cancel over the supervisor (`js/tui.py`, `js/supervisor.py:33`).
- Tool trace: one-line `▸ tool` entries with per-tool arg formatting (`js/runtime.py:258`); per-call stats line (latency/finish/tok/s/ttft/cache%) (`js/runtime.py:1074`).
- Attachments: `@path` / `-f` file+image attachments with history-safe stubs (`js/attach.py:69`, `js/attach.py:106`).
- Model/provider picker as a Textual list UI (`js/picker.py:262`), disabled inside --tui (`js/tui.py:204`).
- Transcript log + full byte-honest request autolog per session (`js/cli.py:382`, `js/settings.py:168`).
- Completion: commands, setting keys, provider names, paths, hunspell spell suggestions (`js/replcomplete.py`).
- Event surface for scripting: input/prompt/stream/tool_call/tool_result/response/turn_start/turn_end/error/cancel/idle (`js/events.py:9`).
- ABSENT: diff rendering for edits, syntax highlighting of code in tool results, session switcher UI, themes, keybinding config, status bar (js `_refresh_status` is a stub `js/tui.py:386`), mouse support decisions, notifications, scrollback search, image display, external editor hook, vim mode, paste handling for images, per-message navigation.

## 5. Extensibility / Scripting

### What js does today

- Agents are prompt directories (`NN-*.md` + `00-tools.yaml` manifest: tools/model/secondary_model/sampling/max_tokens), layered repo < global < project (`js/persona.py:207`, `js/persona.py:239`).
- Config is a script: jsrc files of `set k v` lines, presets (`--preset name` -> jsrc.name), env parity for every knob, `/save` snapshot (`js/settings.py:1`, `js/config.py:240`, `js/settings.py:690`).
- ircII lineage begun: `/set`, `/show`, `/on <event> <handler>`, `/load script` (`js/setcmd.py:425`); event handlers are LIMITED to the set/show/on/load verbs (`js/setcmd.py:508`) — no shell, no send, no custom tool registration.
- Inline prompt directives: `{{VAR}}`, `!{sh ...}`, fenced ```!lang blocks, single-pass injection guard (`js/promptexpand.py`).
- The only shell hook in the harness is `compact.pre_hook` (`js/settings.py:212`).
- Subagents: task fan-out + named-agent tools from prompt dirs; depth limit, worker cap, model-lock policy (`js/toolkit/meta.py:445`).
- ABSENT: a real extension model — no way to add a TOOL without editing js source; no lifecycle hooks that run user code (pre/post tool, session start/end, user-prompt-submit); no custom slash commands; no event handlers that execute shell or send input; no plugin loading; no per-project custom tools directory; no SDK.

## Ranked Gaps

(TO FILL — cross-referenced against codex/crush/claude-code/opencode/pi.)

## At Parity Or Fine

(TO FILL.)
