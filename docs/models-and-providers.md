# Models And Providers

`js` uses the Vercel AI Python SDK (`ai`) as the provider adapter. The built-in
default model is `cpa/claude-opus-5-5`, at effort `medium`. Model routing follows two
patterns: a slash prefix (`provider/model-id`) routes directly to the named provider,
matching against known-provider ids and saved logins; an unprefixed id goes to
`PROVIDER`, or with none set, to `BASE_URL` as an OpenAI-compatible endpoint.

## Provider Routing

Configuration precedence is:

1. `js/jsrc`, shipped in the package: the built-in defaults
2. platform `jsrc`
3. project `.js/jsrc`
4. project `.js/jsrc.local`
5. env vars
6. `--extra` CLI flags (may be repeated)

Model env vars:

```bash
export JS_MODEL=cpa/claude-opus-5-5
```

Optional explicit provider config in `jsrc`:

```text
set PROVIDER openai
set BASE_URL http://127.0.0.1:8317/v1
set API_KEY sk-local
```

When `PROVIDER` is set, the provider is constructed explicitly with the given
base URL and API key. When unset, js routes the model id as follows: a slash prefix
(e.g. `cpa/claude-opus-5-5`) matching a known provider or saved login goes directly
to that provider; an unprefixed id with a `BASE_URL` goes there as an
OpenAI-compatible endpoint, and with neither it is an error. Direct
providers use their default endpoint and official SDK env vars (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, etc.).

Provider env overrides (always win over `jsrc` files):

| Variable | Effect |
| --- | --- |
| `JS_PROVIDER` | Overrides `PROVIDER` |
| `JS_BASE_URL` | Overrides `BASE_URL` |
| `JS_API_KEY` | Overrides `API_KEY` |

### Endpoint URLs: `-u` and `JS_BASE_URL`

`js -u` takes an endpoint with no saved login in one string, and
`JS_BASE_URL` takes the same form:

    [wire+]scheme://model[:key]@host[:port]/path

    js -u http://qwen3:27b@box:11434/v1
    js -u http://qwen3.8-27b:sk-foo-bar@box/v1
    js -u https://nex-agi/nex-n2.5-pro:free:sk-or-xxxx@openrouter.ai/api/v1
    js -u anthropic+https://deepseek-v4-pro:sk-xxxx@api.deepseek.com/anthropic

- Wire: `openai+` (chat-completions), `responses+` or `anthropic+`. An unknown
  prefix is an error. With no prefix, a path ending in `/v1` is OpenAI
  chat-completions and anything else is Anthropic. The prefix is stripped from
  the base URL.
- Model and key: everything between `://` and the last `@`. The text after the
  last colon is the key when `secret-patterns` (`defaults/secret-patterns`, or
  `~/.js/secret-patterns`) calls it a secret; otherwise it stays in the model,
  so `:free`, `:27b` and `:Q4_K_M` survive. A key it misses goes in
  `JS_API_KEY`. No key: `JS_API_KEY`, else `ENDPOINT_DEFAULT_KEY`.
- No `@`: the model is `JS_MODEL`; with no `JS_MODEL` it is an error.
- A `[` is an error: the wire goes in the scheme and effort goes in `-r`.
- `-u` beats `JS_MODEL`, `JS_API_KEY` and `JS_PROVIDER`. In `JS_BASE_URL`, a
  model or key that differs from `JS_MODEL` / `JS_API_KEY`, or a wire prefix
  that contradicts `JS_PROVIDER`, stops the run (exit 2) naming both. A plain
  `JS_BASE_URL` with no model and no prefix is a base URL, as before.
- An explicit `-m provider/model` for a provider with a login beats an exported
  `JS_BASE_URL`: the run uses that login's URL and key.
- The run line shows `model=`, `key=set` or `key=none`, and `wire=`; never the
  key.

Official SDK env vars (`AI_GATEWAY_API_KEY`, `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`) are read directly by `ai-python`
providers and do not need to be copied into `provider.*` config.

### Login store and REPL model picker

`js --login` is a terminal provider login flow: the bare command opens a
black-and-white registry picker of saved, env-configured, and known providers.
`js --login <provider>` then uses the provider's established defaults: if an
API key env var already exists it uses that, lists models to validate the
credential, and offers an optional one-turn secondary model test before saving.
Codex still uses OAuth.

```bash
js --login                 # arrow-key provider registry (saved/env/known)
js --login deepseek        # use DEEPSEEK_API_KEY if present, otherwise prompt
js --login ollama          # local Ollama defaults (http://127.0.0.1:11434/v1, key "ollama")
js --login llama.cpp       # local llama.cpp defaults (http://127.0.0.1:8080/v1)
js --login mimo            # Xiaomi MiMo API endpoint
js --login mimo-token-plan # Xiaomi MiMo Token Plan endpoint (SGP default)
js --login openai-codex        # browser OAuth on localhost:1455
js --login openai-codex-device # device-code OAuth; prints URL + code
js --models-edit deepseek      # curate only DeepSeek's already-cached model list
js --logout deepseek           # remove saved DeepSeek login and cached models
```

Successful logins are saved in `~/.js/logins/logins.toml`, and the fetched
model ids are cached in `~/.js/logins/models-cache.json`. Multiple providers
can be logged in at the same time. `--models-edit <provider>` opens the cached
model checklist with every current entry selected and allows adding model ids
without logging in, fetching, or making a model call. `--logout <provider>`
removes that provider and its cache.

`<add custom provider>` in the login picker lets you save arbitrary provider
names backed by an API shape (`openai-completions`, `openai-responses`, or
`anthropic`) plus a base URL and API key.

Inside the REPL, `/model` and `/pick-model` open the model picker. It is a
chooser, not a discovery/configuration UI: it shows only saved provider logins
and the cached models from those logins. Use `/login` or `js --login <provider>`
to add providers, and use the picker `f` binding to refresh the selected saved
provider's model cache.

For quick one-off REPL changes without saving a provider, these commands still
work:

```text
/provider ollama
/provider openai
/baseurl http://127.0.0.1:11434/v1
/apikey ollama
/model model/id
/models 50
```

## Model Override

CLI:

```bash
js -m "model/id" -p "prompt"
```

`-m` / `--model` overrides the effective configured/env model for that run:
layered config and `JS_MODEL`.

Agent manifests may also declare `model:` and `reasoning:` in `agent.yaml`.
A one-shot `js --agent <id> -p` applies both over the jsrc files; only what the
run itself names wins over them: `-m` / `--model`, `--reasoning`, a `JS_*` env
var, or `--extra`. A resumed session stays on the model and effort of its last
stamp. The REPL, `--bench` and `--commit` apply the manifest `model:` unless
the operator has pinned one with `-m` / `--model`, `JS_MODEL`, or a configured
non-default `MODEL`, and do not apply `reasoning:`. Subagent-specific
precedence and lock behavior live in [subagents.md](subagents.md).

REPL:

```text
/model           # open picker
/pick-model      # open picker
/model model/id  # set model directly
```

Direct `/model model/id` changes the in-process REPL state. Picker selection
also updates the active provider/base/key for that REPL session.

## Reasoning Effort

Environment:

```bash
export JS_EFFORT=high
```

CLI:
For OpenAI Codex / ChatGPT OAuth models, reasoning is a separate setting. Use
`JS_EFFORT=xhigh` or `--reasoning xhigh`; do **not** suffix the model id as
`gpt-5.5:xhigh`.

```bash
js --reasoning off -p "prompt"
js --reasoning max -p "prompt"
```

REPL:

```text
/set EFFORT off
/set EFFORT high
/set EFFORT xhigh   # deepseek-native models
```

`EFFORT` takes any one word and sends it as typed; `/set -EFFORT` restores
the provider default. Where js recognizes the model by name (the family tables
in `js/reasoning.py`, Codex, adaptive Claude) a word on the ladder
`off|minimal|low|medium|high|xhigh|max` is snapped to the nearest effort that
model serves (`off` is the ladder's `none`). Any other word, and every word
for a model js does not recognize, goes out verbatim, `off` and `spoon`
included; the endpoint answers for it. The run line shows the effort the
request carries, read back from the built request.

Sending the word as typed is deliberate, not a bug. Custom chat templates
define their own levels, so js never filters an unknown word, and an HTTP 400
from an endpoint that rejects it (`off`, `default`, ...) is the expected result.

DeepSeek's own endpoint gets `max_reasoning_tokens` from
`DEEPSEEK_REASONING_TOKENS_MAX` (default 32000) unless the effort is off. It is
sent through `extra_body`, and the run line shows it under the setting's name.
The `minimax` provider follows MiniMax's own documentation
(<https://platform.minimax.io/docs/api-reference/text-anthropic-api>,
<https://platform.minimax.io/docs/guides/text-generation>): MiniMax-M3.1-Flash
always thinks and gets `thinking: {"type": "adaptive"}` with
`output_config.effort` snapped to `low|medium|high|xhigh|max` (`off` becomes
`low`, since `none` is a 400). MiniMax-M3 gets `thinking: {"type": "adaptive"}`
and the effort word as typed, or `thinking: {"type": "disabled"}` for `off`.
On an OpenAI-compatible endpoint MiniMax-M3.1-Flash takes `reasoning_effort`
with the same snapping.

Every provider on the Anthropic Messages wire (`anthropic`, `anthropic-custom`,
`opencode-go-anthropic`, `minimax`, and any saved login with sdk `anthropic`)
carries the effort as `output_config.effort` (`js/reasoning.py`):

- Claude 4.6 and later, Fable and Mythos get
  `thinking: {"type": "adaptive", "display": "summarized"}` with the effort
  snapped to the stops the model serves (no `xhigh` on 4.6). `off` sends
  `thinking: {"type": "disabled"}`; on the models that always think (Fable,
  Mythos, Claude 5.5 and later) it sends `ANTHROPIC_REQUIRED_EFFORT` (`low`)
  instead, and the run line shows `low`.
- Every other model gets the effort word as typed and no `thinking`.
- `THINKING_BUDGET`, when set, makes `thinking`
  `{"type": "enabled", "budget_tokens": N}` on any model. js never picks a
  budget itself; Claude 4.7 and later answer a budget with a 400. While a
  budget is in force js sends no `TEMPERATURE` or
  `TOP_K`: Anthropic rejects either alongside budget thinking.
- `max_tokens` is required on this wire: `OUTPUT_TOKENS_MAX` when set, else
  the model's known output limit (server cache, then models.dev), else
  `ANTHROPIC_OUTPUT_TOKENS_MAX` (default 64000).

A `thinking` object in `EXTRA_PARAMS` replaces the one js builds.

`CLAUDE_PROXY_INCLUDE_REASONING` (on by default) adds
`include_reasoning: true` to requests on the `cliproxyapi` transport, which
CLIProxyAPI needs to return Claude's thinking. No other endpoint gets it.

`TEMPLATE_KWARGS` (`--template-kwargs k=v,...`) is sent as
`chat_template_kwargs`, for example `enable_thinking=false`, llama.cpp's
per-request thinking switch. A value is read as JSON; one that is not JSON is
a string. An entry that is not `key=value`, or a key given twice, is an error
naming it, and a `-p` run exits 2.

Session replay retains archived reasoning. OpenAI chat-completions transports
(including llama.cpp) replay reasoning on every assistant message, including
final answers, to preserve the generated prompt prefix. Local/custom endpoints
receive `reasoning_content`, which llama.cpp recognizes before applying its chat
template; named vendor endpoints retain their SDK wire format. Models known to
reject replayed reasoning (GLM) still have it stripped at the provider boundary.
Other transports retain the tool-call-only replay policy; some providers require
reasoning on those messages.

Signed reasoning replays whole. An Anthropic thinking block's signature and a
Codex reasoning item's encrypted content are stored on the assistant record as
`reasoning_parts`, with the provider and model they came from in
`reasoning_from`. They are sent back, on every later turn and after resume, only
to that same provider and model; after a model switch the record falls back to
its plain `reasoning_content` and the rules above.

A signature is bound to the history before it (Anthropic's preserved-thinking
check), so js drops the signed parts that follow any edit it makes to earlier
history: after the first tool result that clearing blanks, on the tail a
keep-tail compaction keeps (in memory and on resume), and after a user message
whose attached files are left out of the history. Parts before the edit keep
replaying. If the provider still refuses a replayed signature ("Invalid
`signature` in `thinking` block", or Codex's `invalid_encrypted_content` for an
item another account produced), the request is retried once with no signed
reasoning in the history, and the history keeps none from then on, on disk too.
An Anthropic tool continuation requires its original thinking blocks. If the
provider refuses those blocks, or says the latest assistant message's blocks
"cannot be modified", js recasts that assistant turn's visible replies, calls
and completed results as fresh user context and retries once. Earlier turns
keep their signed blocks, except after a refused signature, which can belong to
any of them; completed tools are not dispatched again. The
rewritten context is persisted.
This also recovers an older tool continuation with no stored blocks when
Anthropic reports that it expected `thinking` or `redacted_thinking` first.

A reply's text, thinking blocks and tool calls replay in the order the response
held them: each tool call of a record with signed reasoning stores
`text_before`, the answer text that came before it, when answer text follows it.
The record's `reasoning_layout` keeps text block lengths and part kinds so
adjacent and empty text blocks retain their positions too. Older records use
the stored offsets.

The `ai` SDK (0.5.2) omits Anthropic `redacted_thinking` in its parser and
serializer. js's Anthropic protocol adapter emits an empty reasoning part
carrying the whole opaque block in provider metadata, stores it in
`reasoning_parts`, and serializes it back as `redacted_thinking` in its original
position. This applies to live tool continuations and resumed sessions
(`tests/test_thinking_wire.py`).

### Reasoning display

`REASONING_DISPLAY` controls presentation in the standard REPL and one-shot mode,
independently of `EFFORT`:

| Value | Display |
|---|---|
| `0` | Hidden |
| `1` | Stream, then collapse to `N tok reasoning` when the answer starts (or a tool-only call completes) — **default** |
| `2` | Stream and leave visible |
| `3` | Stream and leave visible, with the provider's token count; no count when the provider gives none |

Use `/set REASONING_DISPLAY <0-3>` and `/save`; `JS_REASONING_DISPLAY` is its canonical
environment variable. Setting changes apply to subsequent turns. In the standard async screen,
**Ctrl-O** collapses or expands retained reasoning blocks without changing the
input line; a manual toggle overrides auto-collapse for those blocks.

One-shot and blocking modes stream reasoning on **stderr**, separately from
answer stdout. They leave it visible rather than trying to rewrite terminal
scrollback. Reasoning is excluded from the human answer transcript, but its
original text stays in the append-only session JSONL at every display level,
including partial reasoning received before cancellation. Display controls do
not change next-turn or resumed provider replay.

## Sampling

Sampling overrides are typed per turn and are never exported back into
`os.environ`. Leave a value unset to let the provider/model default win.

Config/script and REPL:

```text
set TEMPERATURE 0.6
set TOP_P 0.95
set TOP_K 64
set MIN_P 0.05
set REPETITION_PENALTY 1.05
set PRESENCE_PENALTY 1.2
set DRY_MULTIPLIER 0.8
set DRY_SEQUENCE_BREAKERS ["\n", ":"]
```

The sampling settings are `TEMPERATURE TOP_P TOP_K MIN_P TOP_N_SIGMA
XTC_PROBABILITY XTC_THRESHOLD DRY_MULTIPLIER DRY_BASE DRY_ALLOWED_LENGTH
DRY_PENALTY_LAST_N DRY_SEQUENCE_BREAKERS REPETITION_PENALTY PRESENCE_PENALTY`.
Each is also a flag (`--temperature`, `--min-p`, `--dry-sequence-breakers`,
...), an env var (`JS_MIN_P`, ...) and an agent.yaml `sampling:` key in lower
case. A value that is not one the setting takes is an error naming it.

Agent manifests (`agent.yaml`) may set the same keys:

```yaml
sampling:
  temperature: 0.6
  min_p: 0.05
  dry_sequence_breakers: ["\n", ":"]
```

Precedence for a turn is:

1. `jsrc` set-script sampling
2. agent manifest `sampling:`
3. `JS_*` sampling env vars
4. flags (`--temperature ...`, `--extra TEMPERATURE=...`), REPL `/set ...`

Every set field is sent; an unset one never is. The fields a wire's SDK takes
as its own parameters go there, every other one goes in `extra_body` under its
lower-case name, which puts it at the top of the request body all the same:

- OpenAI chat wires: `temperature`, `top_p`, `presence_penalty` as parameters.
- Responses wires (`custom_responses`, Codex): `temperature`, `top_p`.
- Anthropic wires: none (the anthropic SDK's stream call takes no sampling
  parameter), so every field rides `extra_body`.

A server that rejects a field answers with its own error.

## Max Output Tokens

Order:

1. `--max-out` or `/set OUTPUT_TOKENS_MAX <tokens>`
2. `JS_OUTPUT_TOKENS_MAX` (or the older `JS_MAX_OUTPUT_TOKENS`)
3. `OUTPUT_TOKENS_MAX` in `jsrc`
4. agent manifest `max_tokens:` in `agent.yaml`
5. unset: no cap is sent, except on the Anthropic wire, which requires one and
   gets the model's known output limit, else `ANTHROPIC_OUTPUT_TOKENS_MAX`

A reply cut off by this cap is sent again once with
`OUTPUT_TOKENS_RETRY_MAX` tokens (default 64000, never above the model's
known limit or the room the window leaves), unless its text is already
printed, then gets up to `OUTPUT_RESUMES_MAX` resume nudges
(default 3). See the runtime loop in `technical-guide.md`.

For custom providers js first tries the active provider mapped to its underlying
models.dev provider id; if that misses, it pattern-matches the model id against
the models.dev catalog so wrappers like `deepseek-v4-pro:cloud` can still pick
up the underlying model limits.

js keeps a local writable mirror of the models.dev catalog in
`~/.js/cache/modelsdotdev/`. On model-limit
lookups it checks the catalog age and refreshes it automatically when it is more
than 8 hours old. To force it immediately:

```bash
js --refresh-model-catalog
```

Inside the REPL:

```text
/refresh-model-catalog
```

## Built-in Provider Support

`js` uses `ai-python`/models.dev when available and adds local/custom shortcuts
for providers that need a friendlier first-class login name:

| Provider id | Notes |
| --- | --- |
| `deepseek` | DeepSeek direct API. Append-only tool-call history, `max_reasoning_tokens=32000` for reasoning. `DEEPSEEK_API_KEY` in the environment auto-selects this provider with `deepseek-flash`. |
| `llama` | Llama API gateway endpoint. |
| `llama.cpp` / `llamacpp` | Local llama.cpp OpenAI-compatible shortcut at `http://127.0.0.1:8080/v1`. |
| `opencode-go` | opencode.ai Zen "go" plan over the **OpenAI-compatible** transport (`sdk=openai`, base `https://opencode.ai/zen/go/v1`, key env `OPENCODE_GO_API_KEY`, base env `OPENCODE_GO_BASE_URL`). Model list is filtered to the GLM/Kimi/DeepSeek/MiMo set this transport serves. |
| `opencode-go-anthropic` | Same Zen "go" plan and API key over the **Anthropic-compatible** transport (`sdk=anthropic`, base `https://opencode.ai/zen/go`, base env `OPENCODE_GO_ANTHROPIC_BASE_URL`). Model list is filtered to the MiniMax/Qwen set this transport serves. |
| `opencode` | OpenCode Zen registry provider (distinct from the `opencode-go` plan above). |
| `ollama` | Local Ollama shortcut; user-facing first-class provider backed by the OpenAI-compatible SDK shape at `http://127.0.0.1:11434/v1`. Key env `OLLAMA_API_KEY` / `OLLAMA_LOCAL_API_KEY`, base env `OLLAMA_BASE_URL` / `OLLAMA_LOCAL_BASE_URL`, model env `OLLAMA_MODEL` / `OLLAMA_LOCAL_MODEL`. No API key required. |
| `ollama-cloud` | Hosted Ollama route on the `ollama` transport over the OpenAI SDK shape (`sdk=openai`, base `https://ollama.com/v1`). Key env `OLLAMA_CLOUD_API_KEY`, base env `OLLAMA_CLOUD_BASE_URL`, model env `OLLAMA_CLOUD_MODEL`. |
| `minimax` | MiniMax direct API over the **Anthropic-compatible** transport (`sdk=anthropic`, base `https://api.minimax.io/anthropic/v1`). Key env `MINIMAX_API_KEY`, base env `MINIMAX_BASE_URL`, model env `MINIMAX_MODEL`. |
| `mimo` / `xiaomi` | Xiaomi MiMo API endpoint at `https://api.xiaomimimo.com/v1`. |
| `mimo-token-plan*` / `xiaomi-token-plan-*` | Xiaomi MiMo Token Plan endpoints; SGP is the default shortcut, AMS/CN variants are explicit. |
| `openai` | Generic OpenAI-compatible endpoint for OpenAI, proxies, and custom compatible servers. |
| `openai-codex` | ChatGPT/Codex OAuth provider. `js --login openai-codex` opens the browser PKCE flow; `js --login openai-codex-device` uses the device-code flow. Tokens live only in the private login store. Runtime uses the Codex Responses endpoint at `https://chatgpt.com/backend-api/codex/responses`. |
| `anthropic` | Anthropic direct API. |
| `omp` | Oh My Pi gateway over the **OpenAI-compatible** transport (`sdk=openai`, no built-in base URL). Key env `OMP_API_KEY` / `OMP_GATEWAY_API_KEY`, base env `OMP_BASE_URL` / `OMP_GATEWAY_BASE_URL`, model env `OMP_MODEL` / `OMP_GATEWAY_MODEL`. No API key required, but a base URL must be set (see below). |
| `cliproxyapi` | CLIProxyAPI local proxy on its own `cliproxyapi` transport over the OpenAI SDK shape (`sdk=openai`, no built-in base URL, custom login shape). Key env `CLIPROXYAPI_API_KEY` / `CLIPROXY_API_KEY`, base env `CLIPROXYAPI_BASE_URL` / `CLIPROXY_BASE_URL`, model env `CLIPROXYAPI_MODEL` / `CLIPROXY_MODEL`. A base URL must be set (see below). |

The `omp` and `cliproxyapi` provider ids are surfaced through the login store:
the listed key/base/model environment variables are read in order, first
non-empty wins. There is no `omp-gateway` provider id — `OMP_GATEWAY_*` is just
the second-priority env-var family for the single `omp` provider.

Because `omp` and `cliproxyapi` ride another vendor's SDK (`sdk=openai`) under
their own provider id, js refuses to route them with no base URL. With
`base_url` unset the OpenAI SDK would silently fall back to its own default
endpoint (`api.openai.com`) and `OPENAI_API_KEY`, sending the prompt and key to
the wrong service, so the runtime raises instead and tells you to run
`js --login <provider>` or set the provider's base-url env var.

`openai-responses` custom logins are still stored as a first-class shape but
route through the OpenAI-compatible chat-completions adapter in `ai==0.2.0`.

### opencode-go transport split and filtered model sets

opencode.ai's Zen "go" plan is exposed as two first-class logins that share one
API key (`OPENCODE_GO_API_KEY`) but route over different transports:

- `opencode-go` uses the OpenAI-compatible adapter (`sdk=openai`) at
  `https://opencode.ai/zen/go/v1`.
- `opencode-go-anthropic` uses the Anthropic-compatible adapter
  (`sdk=anthropic`) at `https://opencode.ai/zen/go`.

Every request to either one carries an `x-opencode-session` header: the
session's cache key (`js-<agent>-<session>`), or a one-off id for a request
with no session. opencode routes and caches by that header, and the Anthropic
endpoint answers a request without it with 400 `MissingSessionID`.

Both endpoints advertise their live catalog through the API. js does not apply a
client-side allow-list — the endpoint is the source of truth, so the JSON bridge
and the login picker surface exactly what the upstream `list_models` returns
(including freshly shipped ids like `glm-5.2` that a curated tuple would hide):

```bash
python -m js --models-json opencode-go
# {"models": [...whatever https://opencode.ai/zen/go/v1 serves right now...]}
```

There is no model gate: a model id is passed straight through to the provider,
which is the one authority on whether it serves it (and 400s with its own message
if it does not).

### JSON bridge commands

Three read-only commands expose the provider/login state as JSON for external
pickers (e.g. the Go picker), all printing a single line to stdout:

- `python -m js --providers-json` — every known provider as
  `{"id", "name", "source"}`, where `source` is `login` (saved), `env`
  (configured via env vars), `registry` (known but unconfigured), or `custom`
  (saved login with no built-in def).
- `python -m js --logins-json` — only saved logins, with `has_api_key` /
  `has_codex_refresh_token` booleans. The Codex refresh token is always nulled
  in the output, and Codex providers' `provider_api_key` is nulled too.
  With `PARANOID_MODE on` or `extra`, API keys and xAI refresh tokens are
  nulled and provider headers are empty. With it off, other providers'
  credentials remain available for a picker to reconnect.
- `python -m js --models-json <provider>` — the (filtered) model list for a
  provider; omit the provider to use the effective configured provider.

## Claude Tool Name Handling

If the actual model string contains `claude` case-insensitively, provider-facing
schemas are transformed:

```text
read  -> Read
write -> Write
task  -> Task
```

Only those three names are changed. This is not based on endpoint or provider
URL. It works through proxies because the check is literally the model name.

Internally:

- registry names stay canonical lowercase
- dispatch resolves provider-facing names through aliases
- persisted session history stores canonical lowercase tool names

This keeps Claude from leaning on Anthropic harness-default names while keeping
the rest of the runtime stable.

## Vision

`vision_enabled_for_model(model, settings)` chooses whether `read` should send
image bytes for image files.

Order:

1. the resolved `VISION` setting: config < `JS_VISION` < `--extra`, followed
   by live `/set VISION on|off` changes. `/set -VISION` restores detection.
2. models.dev input modalities for the model id, keyed on the model rather than
   the provider
3. curated model-name hints, minus anti-hints for code/embed/rerank/audio/
   image-generation names

There is no public `ai-python` model-capability registry — when models.dev has
never heard of the id, the harness falls back to the name-based check.

`read` behavior:

- vision disabled: returns a `VISUAL_FILE ... vision disabled` text stub
- vision enabled: returns an internal image marker
- runtime sends image bytes once for that turn
- session history persists only a text stub

## Shell And Terminal Tools

The `shell` tool runs commands with the `SHELL_PROGRAM` setting (default
`bash`). `set SHELL_PROGRAM zsh` in jsrc makes it zsh.

Current behavior:

- `fs_search` invokes the pinned `tools/bin/rg` installed by `just install`,
  falling back to PATH only when the managed binary is absent.
- `browse` likewise prefers the verified manual `tools/bin/obscura` copy.
- Byte transfers from `fetch` and the pinned tool installer resolve the system
  `aria2c` through the same managed-path-then-PATH resolver. Ordinary API calls
  and rendered browsing do not spawn it.
- `shell` can run `rg`, `fzf`, `bat`, or anything else on PATH when installed.
