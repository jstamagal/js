# Configuration And Sessions

Configuration is read by `js.config.from_env()` at run start and stored in a
frozen `Config` dataclass. CLI flags override individual runs where supported.

## Config Files And Precedence

`js` reads config in this order, lowest to highest:

1. built-in defaults
2. platform `jsrc`
3. project `.js/jsrc`
4. project `.js/jsrc.local`
5. env vars
6. `--extra` CLI flags (may be repeated)

In one line: built-in defaults < platform `jsrc` < project `.js/jsrc` < project
`.js/jsrc.local` < env vars < `--extra` CLI flags.

A `jsrc` file is a config script: each non-comment line is
`set <key> <value>`, using the same dotted keys as the REPL. Comments start with
`#`. First run writes `~/.config/js/jsrc` as a set-script template with stock
defaults and commented reference lines for the remaining registered knobs.

Stock template lines include:

```text
set model.id deepseek/deepseek-v4-flash
set wiki.aliases.creative ~/wiki-creative
set wiki.aliases.general ~/wiki-general
```

Map-valued keys can be extended by setting sub-keys, so
`set wiki.aliases.work /path` adds or overrides only that alias.

`provider.id`, `provider.base_url`, and `provider.api_key` are `<none>` by
default. When `provider.id` is set, the provider is constructed explicitly with
the given base URL and API key; otherwise `ai-python` routes the model id
natively through AI Gateway or via `provider:model` syntax for direct providers.

## Full Key Reference

Every settable key comes from `js/settings.py` `REGISTRY`. The table uses
dotted `set` names and `show` rendering for defaults. Empty-state rendering uses
`off` for false booleans, `<none>` for no-value knobs, and `<unset>` for knobs
that explicitly defer to provider defaults. A set `provider.api_key` is masked
as `<set>`.

| Key | Default | Meaning |
| --- | --- | --- |
| `model.id` | `deepseek/deepseek-v4-flash` | Default model id; unprefixed ids route through AI Gateway. |
| `model.max_output_tokens` | `<none>` | Per-call max_tokens; unset = models.dev metadata when known, else no explicit cap. |
| `model.reasoning_effort` | `<none>` | Thinking effort: off\|low\|medium\|high\|max\|xhigh. |
| `provider.id` | `<none>` | Explicit js provider id (e.g. deepseek, openai-codex, ollama). |
| `provider.base_url` | `<none>` | Explicit provider base URL; unset = provider default. |
| `provider.api_key` | `<none>` | Explicit provider API key; unset = env/login default. |
| `provider.extra` | `<none>` | Free-form extra params passed through to the provider SDK. |
| `limits.max_tool_iterations` | `50` | Max tool calls per turn before the loop gives up. |
| `limits.max_bash_output_bytes` | `262144` | Hard cap on shell stdout per call. |
| `limits.max_tool_result_bytes` | `262144` | Hard cap on any tool result string. |
| `limits.fetch_timeout_s` | `15` | fetch() per-request timeout in seconds. |
| `limits.max_read_lines` | `2000` | Maximum lines returned by read(). |
| `limits.max_line_chars` | `2000` | Maximum characters shown per read/search line. |
| `limits.jsonl_max_line_chars` | `65536` | Maximum characters shown per read line for .jsonl files only. |
| `limits.max_file_bytes` | `2000000` | Maximum file bytes read by fs tools. |
| `limits.task_max_depth` | `2` | Maximum recursive task/subagent depth. |
| `limits.wiki_vault_lock_timeout_s` | `30` | Wiki vault lock timeout in seconds. |
| `runtime.debug` | `off` | Append per-event records to `state/<agent>/debug.log`. |
| `runtime.trace` | `on` | Pretty-print the tool-call trace line as the model runs. |
| `compact.auto` | `on` | Automatic cache-aware context compaction. |
| `compact.context_window` | `<none>` | Context window tokens for fullness math; unset = models.dev metadata. |
| `compact.notify_threshold` | `0.5` | Notify once when context reaches this fraction. |
| `compact.trigger_threshold` | `0.8` | Auto-compact at this fullness fraction. |
| `compact.force_threshold` | `0.9` | Force compact at this fullness fraction. |
| `compact.tail_tokens` | `16384` | Recent tail budget retained after compaction. |
| `compact.min_savings_tokens` | `400` | Skip compaction unless estimated savings exceeds this. |
| `compact.chars_per_token` | `4.0` | Fallback/self-calibrating character-to-token estimate. |
| `compact.model` | `same` | Model used to write the compaction summary; 'same' = active model. |
| `compact.summary_max_tokens` | `4096` | Max tokens for the compaction summary (hard-capped at 8192). |
| `compact.pre_hook` | `<none>` | Optional shell command whose stdout guides compaction. |
| `subagents.prefer_inherit` | `off` | Subagents inherit the parent's model when true; else use the agent's own primary. |
| `subagents.lock_model` | `off` | When true, the main agent cannot pick a subagent model via the task tool. |
| `tools.alias_profiles` | `<none>` | Model-facing tool-name alias profiles: list of {match:[...], aliases:{...}}. |
| `wiki.aliases` | `<none>` | Vault alias map; set sub-keys, e.g. `set wiki.aliases.creative /path`. |
| `artifact.dir` | `<none>` | Artifact library directory. |
| `artifact.url` | `<none>` | Artifact HTTP base URL. |
| `artifact.bin` | `<none>` | Artifact CLI binary. |

Artifact config values are unset by default in `jsrc`. Artifact helpers resolve
directory, URL, and binary with this precedence: `set artifact.*` in `jsrc`,
then the matching `ARTIFACT_*` environment variable, then the built-in default
(`/srv/artifacts`, `http://localhost`, or `artifact`).

Wiki vault aliases come from config via `set wiki.aliases.<name> <path>`; the
stock `jsrc` defines `creative` as `~/wiki-creative` and `general` as
`~/wiki-general`. `--vault` may name a configured alias or a path. Without
`--vault`, wiki mode infers a vault only by walking up from the target path or
current directory and finding a `PURPOSE.md` sentinel or a directory whose name
matches `wiki-*`. If nothing resolves, the run stops with an error. There is no
default vault.

## Environment Variables

Registry-backed `JS_*` variables overlay all `jsrc` files and use the same
coercion as `set`.

| Variable | Key | Default | Meaning |
| --- | --- | --- | --- |
| `JS_MODEL` | `model.id` | `deepseek/deepseek-v4-flash` | Default model id; unprefixed ids route through AI Gateway. |
| `JS_MAX_OUTPUT_TOKENS` | `model.max_output_tokens` | `<none>` | Per-call max_tokens; unset = models.dev metadata when known, else no explicit cap. |
| `JS_REASONING` | `model.reasoning_effort` | `<none>` | Thinking effort: off\|low\|medium\|high\|max\|xhigh. |
| `JS_PROVIDER` | `provider.id` | `<none>` | Explicit js provider id (e.g. deepseek, openai-codex, ollama). |
| `JS_BASE_URL` | `provider.base_url` | `<none>` | Explicit provider base URL; unset = provider default. |
| `JS_API_KEY` | `provider.api_key` | `<none>` | Explicit provider API key; unset = env/login default. |
| `JS_MAX_TOOL_ITERATIONS` | `limits.max_tool_iterations` | `50` | Max tool calls per turn before the loop gives up. |
| `JS_MAX_BASH_OUTPUT_BYTES` | `limits.max_bash_output_bytes` | `262144` | Hard cap on shell stdout per call. |
| `JS_MAX_TOOL_RESULT_BYTES` | `limits.max_tool_result_bytes` | `262144` | Hard cap on any tool result string. |
| `JS_FETCH_TIMEOUT` | `limits.fetch_timeout_s` | `15` | fetch() per-request timeout in seconds. |
| `JS_JSONL_MAX_LINE_CHARS` | `limits.jsonl_max_line_chars` | `65536` | Maximum characters shown per read line for .jsonl files only. |
| `JS_DEBUG` | `runtime.debug` | `off` | Append per-event records to `state/<agent>/debug.log`. |
| `JS_TRACE` | `runtime.trace` | `on` | Pretty-print the tool-call trace line as the model runs. |

Official `ai-python` SDK env vars (`AI_GATEWAY_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_BASE_URL`) are read directly by the provider and
do not need to be copied into `jsrc`.

Agent/session env remains accepted for compatibility (`JS_AGENT`, `JS_SESSION`),
but CLI code threads selected agent/session through `Config` instead of mutating
`os.environ`. Wiki/artifact mode is threaded through `ToolContext`; env fallbacks
remain only for direct tool compatibility and subprocess boundaries.

The byte caps use the canonical `_BYTES` env names only
(`JS_MAX_BASH_OUTPUT_BYTES`, `JS_MAX_TOOL_RESULT_BYTES`); there are no shorter
aliases. Note there is **no** env var for `limits.task_max_depth` — set it in
`jsrc` or via `--extra limits.task_max_depth=N`.

`JS_ALLOW_INLINE_CODE=1` permits code-running inline prompt directives; it is set
automatically by `--dangerously-evaluate-inline-code`. See
[inline-directives.md](inline-directives.md).

## CLI Overrides

Common flags:

```bash
js -m "model/id"
js --agent autocoder
js --session existing-session
js --no-save
js --debug
js --debug-file /tmp/js-debug.log
js --reasoning off
js --max-out 64000
js --extra limits.task_max_depth=3
js --dangerously-evaluate-inline-code
js --migrate-config
```

`--extra KEY=VALUE` sets any dotted config key for one run and wins over env and
all `jsrc` files. It may be repeated. Values are coerced int -> float ->
`true`/`false`/`null` -> string; the key splits on the first `=` only, so values
may contain `=`.

In the REPL, `set [key [val]]` uses the same registry: `set` lists knobs,
`set key` shows one value, and `set key value` changes the live setting.
`show [key]` lists every current value or only the requested key. Secret values
such as `provider.api_key` render as `<set>` once set.

The command runner works slashless: `js/setcmd.py` `_normalize` strips a single
optional leading `/`, so `set` and `/set` (and `show`/`/show`) dispatch
identically in the REPL. The same runner backs REPL commands, `jsrc` config
loading, and `/load`ed runtime scripts, but the accepted verb set differs by
entry point. `jsrc` lines are the bare `set <key> <value>` form and accept only
`set`; `apply_config_line` rejects every other verb. Runtime scripts loaded with
`/load <file>` currently accept `set`, `show`, nested `load`, and `on`, with
nested script paths resolved relative to the file that contains them. Registered
event handlers run through that same runtime-script command surface when an
event is emitted. Handler failures are recorded on the event emission and in
debug telemetry rather than raised through the model loop; recursive event
dispatch from inside a handler is skipped.

`--migrate-config` is a one-shot conversion for a legacy `config.toml`: it
writes equivalent `set ...` lines to `jsrc` and exits. The migration path is
temporary and is removed after 2 releases.

`--dangerously-evaluate-inline-code` (alias `--dangerously-evaluate-shell-commands`)
enables the code-running inline prompt directives. It compiles and runs arbitrary
code from prompt files -- see [inline-directives.md](inline-directives.md).

`--reasoning off` is an explicit override. It disables reasoning even when
`JS_REASONING` is set.

`-m` / `--model` overrides the effective configured/env model for the selected
run or session.

`--max-out` overrides the configured max output tokens for that run. If max
output is unset, the runtime asks models.dev for the active model limit and
otherwise leaves the provider cap alone.
`--refresh-model-catalog` forces an immediate refresh of js's local models.dev
mirror, writes the refreshed timestamp to platform data, and exits unless you
also requested another action such as `--prompt`.

## Agent Directories

Config lives in the platform config directory and runtime/session state in the
platform data directory (resolved by `platformdirs`; on Linux these honor
`$XDG_CONFIG_HOME`/`$XDG_DATA_HOME`, defaulting to `~/.config/js` and
`~/.local/share/js`). Project-local `.js/` files stay with the project.

Platform config directory:

```text
<config-dir>/            # e.g. ~/.config/js
  jsrc
  logins.toml
  models-cache.json
  AGENTS.md
  AGENTS.local.md
  agents/<agent_id>/     # global agent prompts
  skills/
```

Platform data directory:

```text
<data-dir>/              # e.g. ~/.local/share/js
  sessions/<agent_id>/
    .history
    latest.json
    <session>.jsonl
  state/<agent_id>/
    debug.log
```

The `jsrc` template is written on first run; the per-agent `sessions/`
and `state/` directories are created lazily when an agent runs. The agent id is
validated (`^[A-Za-z0-9_-]+$`) *before* any directory is created, so a bad id
never leaves stray files.

### Agent prompts

Agent prompts are discovered from repo `prompts/`, global `agents/` in the
platform config dir, and project `.js/agents/`; project scope wins over global,
which wins over repo. `AGENTS.md` and `AGENTS.local.md` from global then project
scope are prepended to every main-agent and subagent system prompt, blank-line
separated.

For the full how-to — creating global/project agents, id rules, reserved names,
the `00-*` zero file, and `tools:` frontmatter — see
[agents-and-prompts.md](agents-and-prompts.md). The assembled system prompt is
also run through inline-directive expansion
([inline-directives.md](inline-directives.md)) before it reaches the model.

## Session Resolution

Without `--session`, saved runs reserve a new unique JSONL file under the
selected agent's `sessions/` directory. `--no-save` uses `os.devnull`.

With `--session`, the session must already exist and resolve inside the selected
agent's session directory:

```bash
js --session 20260611T120000Z-abcd -p "continue"
js --session 20260611T120000Z-abcd.jsonl -p "continue"
```

Absolute session paths are accepted only when they are existing `.jsonl` files
inside that agent's sessions directory. Relative traversal is rejected after
resolution.

## JSONL Record Shape

The memory file is append-only JSONL. Records have:

```json
{"kind":"message","ts":1781190000.0,"version":1,"message":{"role":"user","content":"..."}}
{"kind":"mark","ts":1781190001.0,"version":1,"marker":"session_reset"}
```

`load_messages()` ignores:

- malformed JSON lines
- unknown versions
- unknown kinds
- messages whose role is not `user`, `assistant`, `tool`, or `system`

The writer uses `fcntl` locks and `fsync` on append.

## Control Marks

`session_reset` clears the loaded message list at that point in the file.

`rollback_to:N` truncates the loaded message list to `N`. The REPL writes this
when a turn is aborted or a runtime exception happens after the user message was
already appended.

`compaction:{...}` marks rebuild loaded context as one `<compaction-summary>` user message plus a safe tail. Other marks are ignored by the loader but remain in JSONL as audit notes.

## Wipe And Backups

`/wipe` rotates the active file:

```text
session.jsonl      -> session.jsonl.bak
session.jsonl      -> session.jsonl.bak.1
session.jsonl      -> session.jsonl.bak.2
```

Existing backups are preserved. The in-process REPL message list is cleared.

## Compaction

Compaction is append-only: the JSONL file is never rewritten. `/compact [focus]`
and `/compact up to here` append a compaction mark; `js --compact <session>` does
the same offline. On load, the mark rebuilds in-memory context as:

1. the unchanged system prompt,
2. one user message containing `<compaction-summary>...`,
3. a fixed tail (`tail_tokens`, default `16384`) whose boundary backs up so an
assistant `tool_calls` message is not separated from its tool results.

The summary model is `compact.model`; literal `same` uses the active session
model. Optional focus text and `compact.pre_hook` stdout are supplied as
guidance. Hook failures warn but do not block compaction.
