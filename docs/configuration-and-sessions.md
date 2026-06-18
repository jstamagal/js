# Configuration And Sessions

Configuration is read by `js.config.from_env()` at run start and stored in a
frozen `Config` dataclass. CLI flags override individual runs where supported.

## Config Files And Precedence

`js` reads config in this order, lowest to highest:

1. built-in defaults
2. the platform config `config.toml`
3. project `.js/config.toml`
4. project `.js/config.local.toml`
5. environment variables
6. CLI flags and repeated `--extra key.path=value`

First run writes the platform config `config.toml` as a fully commented template (every knob,
each commented out with its default). A fuller annotated reference is checked in
at [config.example.toml](config.example.toml) — copy blocks from there into any
of the three config files.

Tables are deep-merged across layers, so a later file can override one nested
key (for example a single entry in `[model.reasoning_effort]`) without replacing
the whole table.

Core defaults include:

```toml
[model]
id = "deepseek/deepseek-v4-flash"

[limits]
max_tool_iterations = 50
max_bash_output_bytes = 262144
max_tool_result_bytes = 262144
fetch_timeout_s = 15
wiki_vault_lock_timeout_s = 30

[compact]
auto = true
model = "same"
# context_window = unset
tail_tokens = 16384
min_savings_tokens = 400

[artifact]
dir = "/srv/artifacts"
url = "http://localhost"
bin = "artifact"
```

`[provider] id`, `[provider] base_url`, and `[provider] api_key` are unset by
default. When `[provider] id` is set, the provider is constructed explicitly
with the given base URL and API key; otherwise `ai-python` routes the model id
natively through AI Gateway or via `provider:model` syntax for direct providers.

## Full Key Reference

Every key the harness understands (from `js/settings.py` `_KNOWN_KEYS`). Keys
shown as `unset` are passed through only when present.

| Key | Default | Meaning |
| --- | --- | --- |
| `model.id` | `deepseek/deepseek-v4-flash` | Model id; unprefixed ids route through AI Gateway. |
| `model.max_output_tokens` | unset | Per-call `max_tokens`; unset -> models.dev metadata when known, otherwise no explicit cap is sent. |
| `model.reasoning_effort` | unset | Thinking effort: `off`/`low`/`medium`/`high`/`max`/`xhigh` (`max`->high, `off`/`none`->disabled). |
| `limits.max_tool_iterations` | `50` | Max tool calls per turn before the loop gives up. |
| `limits.max_bash_output_bytes` | `262144` | Hard cap on shell stdout per call. |
| `limits.max_tool_result_bytes` | `262144` | Hard cap on any tool result string. |
| `limits.fetch_timeout_s` | `15` | `fetch()` per-request timeout in seconds. |
| `limits.max_read_lines` | `2000` | Max lines returned by `read`. |
| `limits.max_line_chars` | `2000` | Max characters shown per read/search line. |
| `limits.max_file_bytes` | `2000000` | Max file bytes read by filesystem tools. |
| `limits.task_max_depth` | `2` | Max recursive task/subagent depth. No env var — set here or via `--extra`. |
| `limits.wiki_vault_lock_timeout_s` | `30` | Wiki vault lock timeout in seconds. |
| `runtime.debug` | `false` | Append per-event records to the platform data `state/<agent>/debug.log` (env `JS_DEBUG`). |
| `runtime.trace` | `true` | Pretty-print the live tool-call trace line (env `JS_TRACE`). |
| `provider.id` | unset | Explicit `ai-python` provider id (e.g. `openai`, `anthropic`). |
| `provider.base_url` | unset | Explicit provider base URL; unset = provider default endpoint. |
| `provider.api_key` | unset | Explicit provider API key; unset = official SDK env var. |
| `compact.auto` | `true` | Enable automatic cache-first compaction. |
| `compact.model` | `"same"` | Summarizer model; `"same"` = active session model. |
| `compact.context_window` | unset | Context-window tokens for fullness math; unset -> models.dev metadata when known. |
| `compact.notify_threshold` | `0.50` | Notify once when context reaches this fraction. |
| `compact.trigger_threshold` | `0.80` | Auto-compact at this fullness fraction. |
| `compact.force_threshold` | `0.90` | Force compaction at this fullness fraction. |
| `compact.tail_tokens` | `16384` | Recent tail budget kept verbatim after compaction. |
| `compact.min_savings_tokens` | `400` | Skip compaction unless estimated savings exceed this. |
| `compact.chars_per_token` | `4.0` | Fallback/self-calibrating char-to-token estimate. |
| `compact.pre_hook` | unset | Shell command whose stdout guides compaction; failures warn only. |
| `wiki.aliases` | `{}` | Vault alias map, e.g. `creative = "/path/to/wiki"` (use `[wiki.aliases]`). |
| `artifact.dir` | `/srv/artifacts` | Artifact library directory. |
| `artifact.url` | `http://localhost` | Artifact HTTP base URL. |
| `artifact.bin` | `artifact` | Artifact CLI binary. |

## Environment Variables

Provider and model:

| Variable | Meaning | Default |
| --- | --- | --- |
| `JS_MODEL` | env override for `[model].id` | `deepseek/deepseek-v4-flash` |
| `JS_PROVIDER` | env override for `[provider].id` | unset |
| `JS_BASE_URL` | env override for `[provider].base_url` | unset |
| `JS_API_KEY` | env override for `[provider].api_key` | unset |

Sampling overrides:

| Variable | Sampling knob | Sent as |
| --- | --- | --- |
| `JS_TEMP` | temperature | top-level (OpenAI-standard) |
| `JS_TOPP` | top_p | top-level (OpenAI-standard) |
| `JS_PRPEN` | presence_penalty | top-level (OpenAI-standard) |
| `JS_TOPK` | top_k | `extra_body` (vLLM extension) |
| `JS_REPPEN` | repetition_penalty | `extra_body` (vLLM extension) |

When **unset**, js sends no sampling params at all and defers to the
backend/model default (e.g. a local model's `generation_config.json`); this is
the recommended default. Set any subset to override for a run. `top_k` and
`repetition_penalty` merge into `extra_body` alongside any provider-specific
extras (e.g. DeepSeek's `max_reasoning_tokens`). Typical qwen presets —
coding: `JS_TEMP=0.6 JS_TOPK=20 JS_TOPP=0.95 JS_REPPEN=1.0 JS_PRPEN=0.0`;
creative: `JS_TEMP=1.0 JS_TOPK=20 JS_TOPP=0.95 JS_REPPEN=1.1 JS_PRPEN=1.5`.

Official `ai-python` SDK env vars (`AI_GATEWAY_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_BASE_URL`) are read directly by the provider and
do not need to be copied into config.

Agent/session env remains accepted for compatibility (`JS_AGENT`, `JS_SESSION`),
but CLI code threads selected agent/session through `Config` instead of mutating
`os.environ`. Wiki/artifact mode is threaded through `ToolContext`; env fallbacks
remain only for direct tool compatibility and subprocess boundaries.

Runtime env overrides include `JS_REASONING`, `JS_MAX_OUTPUT_TOKENS`,
`JS_MAX_TOOL_ITERATIONS`, `JS_MAX_BASH_OUTPUT_BYTES`,
`JS_MAX_TOOL_RESULT_BYTES`, `JS_FETCH_TIMEOUT`, `JS_DEBUG`, `JS_TRACE`,
`JS_VISION`, `JS_PROVIDER`, `JS_BASE_URL`, `JS_API_KEY`, and the sampling
overrides `JS_TEMP`, `JS_TOPP`, `JS_TOPK`, `JS_REPPEN`, `JS_PRPEN`.

The byte caps use the canonical `_BYTES` env names only
(`JS_MAX_BASH_OUTPUT_BYTES`, `JS_MAX_TOOL_RESULT_BYTES`); there are no shorter
aliases. Note there is **no** env var for `limits.task_max_depth` — set it in
config or via `--extra limits.task_max_depth=N`.

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
```

`--extra KEY=VALUE` sets any dotted config key for one run and wins over env and
all config files. It may be repeated. Values are coerced int -> float ->
`true`/`false`/`null` -> string; the key splits on the first `=` only, so values
may contain `=`.

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
  config.toml
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

The `config.toml` template is written on first run; the per-agent `sessions/`
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

The summary model is `[compact].model`; literal `same` uses the active session
model. Optional focus text and `[compact].pre_hook` stdout are supplied as
guidance. Hook failures warn but do not block compaction.
