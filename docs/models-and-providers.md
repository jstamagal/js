# Models And Providers

`js` uses the Vercel AI Python SDK (`ai`) as the provider adapter. The built-in
default model is `deepseek/deepseek-v4-flash`. There is no built-in proxy route:
unprefixed model ids route through AI Gateway; `provider:model` ids go directly
to the named provider.

## Provider Routing

Configuration precedence is:

1. built-in defaults
2. platform `jsrc`
3. project `.js/jsrc`
4. project `.js/jsrc.local`
5. env vars
6. `--extra` CLI flags (may be repeated)

Model env vars:

```bash
export JS_MODEL=deepseek/deepseek-v4-flash
```

Optional explicit provider config in `jsrc`:

```text
set provider.id openai
set provider.base_url http://127.0.0.1:8317/v1
set provider.api_key sk-local
```

When `provider.id` is set, the provider is constructed explicitly with the given
base URL and API key. When unset, `ai.get_model(model_id)` is called and
`ai-python` routes unprefixed ids through AI Gateway, while `provider:model` ids
(e.g. `openai:gpt-4o`) go to the named provider using its default endpoint and
official SDK env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.).

Provider env overrides (always win over `jsrc` files):

| Variable | Effect |
| --- | --- |
| `JS_PROVIDER` | Overrides `provider.id` |
| `JS_BASE_URL` | Overrides `provider.base_url` |
| `JS_API_KEY` | Overrides `provider.api_key` |

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
js --logout deepseek           # remove saved DeepSeek login and cached models
```

Successful logins are saved in `~/.config/js/logins.toml`, and the fetched
model ids are cached in `~/.config/js/models-cache.json`. Multiple providers
can be logged in at the same time; `--logout <provider>` removes that provider
and its cache.

`<add custom provider>` in the login picker lets you save arbitrary provider
names backed by an API shape (`openai-completions`, `openai-responses`, or
`anthropic`) plus a base URL and API key.

Inside the REPL, `/model` and `/pick-model` open the Textual picker. It is a
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
export JS_REASONING=high
```

CLI:
For OpenAI Codex / ChatGPT OAuth models, reasoning is a separate knob. Use
`JS_REASONING=xhigh` or `--reasoning xhigh`; do **not** suffix the model id as
`gpt-5.5:xhigh`.

```bash
js --reasoning off -p "prompt"
js --reasoning max -p "prompt"
```

REPL:

```text
/set reasoning off
/set reasoning high
/set reasoning xhigh   # deepseek-native models
```

Normalization:

- `max` -> `high`
- `min` -> `low`
- `none`, `off`, `0` -> the literal string `"none"` (explicitly disables reasoning)
- empty/unset -> `None` (provider default applies)
- other values are forwarded as typed (`xhigh` for deepseek-native endpoints)

DeepSeek gets `max_reasoning_tokens=32000` when reasoning is enabled so it can
use its full reasoning budget without capping total output earlier than necessary.
For direct OpenAI-compatible transports this is sent through `extra_body`, not as
an invalid top-level SDK kwarg. MiniMax (token-plan and API variants) strips the
OpenAI-shaped reasoning object because its adapter rejects it.

The runtime round-trips provider reasoning content on assistant messages that
carry tool calls because some reasoning providers require it on the next call.

## Sampling

Sampling overrides are typed per turn and are never exported back into
`os.environ`. Leave a value unset to let the provider/model default win.

Config/script and REPL:

```text
set sampling.temperature 0.6
set sampling.top_p 0.95
set sampling.top_k 64
set sampling.repetition_penalty 1.05
set sampling.presence_penalty 1.2
```

Environment:

```bash
export JS_TEMP=0.6
export JS_TOPP=0.95
export JS_TOPK=64
export JS_REPPEN=1.05
export JS_PRPEN=1.2
```

Agent manifests (`00-tools.yaml`) may set the same keys:

```yaml
sampling:
  temperature: 0.6
  top_p: 0.95
```

Precedence for a turn is:

1. `jsrc` set-script sampling
2. agent manifest `sampling:`
3. `JS_*` sampling env vars
4. CLI/live overrides (`--extra sampling.temperature=...`, REPL `/set ...`)

Wire filtering is provider-family specific:

- Anthropic wires (`anthropic`, `custom_anthropic`) send top-level
  `temperature`, `top_p`, and `top_k`; penalties are dropped.
- OpenAI wires (`openai`, `custom_responses`, `codex_oauth`) send top-level
  `temperature`, `top_p`, and `presence_penalty`; `top_k` and
  `repetition_penalty` are dropped.
- OpenAI-compatible wires (`openai_compatible`, `custom_openai`, `deepseek`,
  `ollama`, `llama.cpp`, `cliproxyapi`) send top-level `temperature`, `top_p`,
  and `presence_penalty`; `top_k` and `repetition_penalty` go in `extra_body`.
- Unknown or SDK-gateway transports send no sampling params.

## Max Output Tokens

Order:

1. `--max-out` or `/set model.max_output_tokens <tokens>`
2. `JS_MAX_OUTPUT_TOKENS`
3. `model.max_output_tokens` in `jsrc`
4. models.dev metadata for the active model/provider
5. if the catalog has no match, no explicit cap is sent

For custom providers js first tries the active provider mapped to its underlying
models.dev provider id; if that misses, it pattern-matches the model id against
the models.dev catalog so wrappers like `deepseek-v4-pro:cloud` can still pick
up the underlying model limits.

js keeps a local writable mirror of the models.dev catalog under platform data
(`~/.local/share/js/modelsdotdev/` on a default Linux setup). On model-limit
lookups it checks the catalog age and refreshes it automatically when it is more
than 72 hours old. To force it immediately:

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
| `deepseek` | DeepSeek direct API. Append-only tool-call history, `max_reasoning_tokens=32000` for reasoning. |
| `llama` | Llama API gateway endpoint. |
| `llama.cpp` / `llamacpp` | Local llama.cpp OpenAI-compatible shortcut at `http://127.0.0.1:8080/v1`. |
| `opencode-go` | opencode.ai Zen "go" plan over the **OpenAI-compatible** transport (`sdk=openai`, base `https://opencode.ai/zen/go/v1`, key env `OPENCODE_GO_API_KEY`, base env `OPENCODE_GO_BASE_URL`). Model list is filtered to the GLM/Kimi/DeepSeek/MiMo set this transport serves. |
| `opencode-go-anthropic` | Same Zen "go" plan and API key over the **Anthropic-compatible** transport (`sdk=anthropic`, base `https://opencode.ai/zen/go`, base env `OPENCODE_GO_ANTHROPIC_BASE_URL`). Model list is filtered to the MiniMax/Qwen set this transport serves. |
| `opencode` | OpenCode Zen registry provider (distinct from the `opencode-go` plan above). |
| `ollama` | Local Ollama shortcut; user-facing first-class provider backed by the OpenAI-compatible SDK shape at `http://127.0.0.1:11434/v1`. |
| `ollama-cloud` | Hosted Ollama route. |
| `minimax` | MiniMax API shape. |
| `minimax-coding-plan` | MiniMax token-plan route. |
| `mimo` / `xiaomi` | Xiaomi MiMo API endpoint at `https://api.xiaomimimo.com/v1`. |
| `mimo-token-plan*` / `xiaomi-token-plan-*` | Xiaomi MiMo Token Plan endpoints; SGP is the default shortcut, AMS/CN variants are explicit. |
| `openai` | Generic OpenAI-compatible endpoint for OpenAI, proxies, and custom compatible servers. |
| `openai-codex` | ChatGPT/Codex OAuth provider. `js --login openai-codex` opens the browser PKCE flow; `js --login openai-codex-device` uses the device-code flow. Tokens live only in the private login store. Runtime uses the Codex Responses endpoint at `https://chatgpt.com/backend-api/codex/responses`. |
| `anthropic` | Anthropic direct API. |

`openai-responses` custom logins are still stored as a first-class shape but
route through the OpenAI-compatible chat-completions adapter in `ai==0.2.0`.

### opencode-go transport split and filtered model sets

opencode.ai's Zen "go" plan is exposed as two first-class logins that share one
API key (`OPENCODE_GO_API_KEY`) but route over different transports:

- `opencode-go` uses the OpenAI-compatible adapter (`sdk=openai`) at
  `https://opencode.ai/zen/go/v1`.
- `opencode-go-anthropic` uses the Anthropic-compatible adapter
  (`sdk=anthropic`) at `https://opencode.ai/zen/go`.

Both endpoints advertise the same upstream catalog, so each provider applies an
`allowed_models` filter (`ProviderDef.filter_models`) to keep only the models its
transport actually serves. The filter is what the JSON bridge surfaces:

```bash
python -m js --models-json opencode-go
# {"models": ["deepseek-v4-flash", "deepseek-v4-pro", "glm-5", "glm-5.1",
#             "kimi-k2.6", "kimi-k2.7-code", "mimo-v2.5", "mimo-v2.5-pro"]}
python -m js --models-json opencode-go-anthropic
# {"models": ["minimax-m2.5", "minimax-m2.7", "minimax-m3",
#             "qwen3.6-plus", "qwen3.7-max", "qwen3.7-plus"]}
```

The same allow-list backs `supports_model`, so selecting a model outside a
provider's set is rejected before a request is sent (`model_client` raises
`does not serve model ...; allowed models: ...`).

### JSON bridge commands

Three read-only commands expose the provider/login state as JSON for external
pickers (e.g. the Go picker), all printing a single line to stdout:

- `python -m js --providers-json` — every known provider as
  `{"id", "name", "source"}`, where `source` is `login` (saved), `env`
  (configured via env vars), `registry` (known but unconfigured), or `custom`
  (saved login with no built-in def).
- `python -m js --logins-json` — only saved logins, with `has_api_key` /
  `has_codex_refresh_token` booleans. The Codex refresh token is always nulled
  in the output, and Codex providers' `provider_api_key` is nulled too, so the
  bridge never leaks long-lived secrets.
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

`vision_enabled_for_model(model)` chooses whether `read` should send image bytes
for image files.

Order:

1. `JS_VISION` explicit bool override
2. curated model-name hints
3. anti-hints for code/embed/rerank/audio/image-generation names

There is no public `ai-python` model-capability registry — the harness uses
a heuristic name-based check.

`read` behavior:

- vision disabled: returns a `VISUAL_FILE ... vision disabled` text stub
- vision enabled: returns an internal image marker
- runtime sends image bytes once for that turn
- session history persists only a text stub

## Zsh And Terminal Tools

The `shell` tool runs through `$SHELL -c` on Unix. If the process environment is
zsh-first, the tool is zsh-first:

```bash
export SHELL=/usr/bin/zsh
```

The harness does not itself require `rg`, `fzf`, or `bat`. Current behavior:

- `fs_search` is implemented in Python with `re`.
- `sem_search` is a local term-ranked search, not embeddings.
- `shell` can run `rg`, `fzf`, `bat`, or anything else on PATH when installed.

If a future port wants closer Forge shell ergonomics, preserve `$SHELL` first
and add optional shell-level affordances without changing canonical tool names.
