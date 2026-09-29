# js

`js` is a personal terminal LLM harness written in Python. It runs interactive
chat, one-shot prompts, pipe workflows, local tools, parallel
subagents, wiki agents, and commit-agent
workflows through the Vercel AI Python SDK (`ai-python`).
This repo is for a power user, not a product team. The design bias is low
friction with lots of settings: direct shell access, explicit sessions, prompt
directories as agents, rich model-facing tool descriptions, and no compatibility
aliases kept around just to make old prompts happy.

## Quick Start

```bash
pip install -e ".[test]"

js
js -p "summarize this repo"
git diff | js -p "review this patch"
js --commit
js --commit /path/to/repo -p "mostly housekeeping"
```

Common built-in modes:

```bash
wiki ingest ~/wiki --unit ~/wiki/inbox/source.md
wiki flow ~/wiki "ingest inbox"
wiki flow ~/wiki "ingest all inbox units"
```

## Documentation

Start here:

- [docs/README.md](docs/README.md): documentation map.
- [docs/user-guide.md](docs/user-guide.md): commands, workflows, and daily use.
- [docs/technical-guide.md](docs/technical-guide.md): architecture and runtime internals.
- [docs/tool-system.md](docs/tool-system.md): registry, schemas, dispatch, and descriptions.
- [docs/tools-reference.md](docs/tools-reference.md): all public tools.
- [docs/subagents.md](docs/subagents.md): `task`, generated agent tools, creating global/project agents, `agent.yaml`, isolation, and limits.
- [docs/inline-directives.md](docs/inline-directives.md): `{{VAR}}` / `!{sub}` / `` ```!lang `` expansion and the inline-code flag.
- [docs/configuration-and-sessions.md](docs/configuration-and-sessions.md): config precedence, full key reference, env vars, sessions, memory, and compaction.
- [docs/models-and-providers.md](docs/models-and-providers.md): ai-python routing, proxies, Claude naming, reasoning, vision.

## Project Map

```text
js/model_client.py                 single `ai` SDK import boundary
js/runtime.py                      streaming loop and tool dispatch
js/toolkit/core.py                Tool, ToolContext, call_tool
js/toolkit/registry.py            registry assembly and selector filtering
js/toolkit/fs.py                  read/write/search/patch/remove/undo
js/toolkit/process_net.py         shell and fetch
js/toolkit/meta.py                plan/skill/task/subagents
js/toolkit/wiki/                  deterministic tools for installed wiki agents
js/toolkit/tool_descriptions/     model-facing tool contracts
prompts/                          repo prompt-directory agents; layered with ~/.js/agents/ and project .js/agents/
                                  (repo `prompts/`, global `~/.js/agents/`, and project `.js/agents/`;
                                  project scope wins over global, which wins over repo)
tests/                            offline, harness, smoke, and live/proxy tests
docs/                             full user and technical documentation
```

## Tool Surface

The public registry exposes canonical tool names only; use the names documented
in [docs/tools-reference.md](docs/tools-reference.md), not compatibility
spellings like `fs_read`, `fs_write`, `cat`, `grep`, or `semantic_search`.

## Config And Model Defaults
Config is a script: each line of a `jsrc` file is a `set <key> <value>` command,
applied at startup. Files layer lowest-to-highest as `js/jsrc` (shipped in the
package: one line per setting, the built-in defaults), `~/.js/jsrc`,
project `.js/jsrc`, then project `.js/jsrc.local`; env vars override files and
CLI `--extra key=value` overrides env. js creates no `~/.js/jsrc`; `/save`
writes it. `js/jsrc` sets `model.id` to `deepseek/deepseek-v4-flash`;
`JS_MODEL` overrides it. Explicit
`set provider.id/base_url/api_key` are opt-in only; `JS_PROVIDER`, `JS_BASE_URL`,
and `JS_API_KEY` are env overrides. Official SDK env vars (`AI_GATEWAY_API_KEY`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`) are read directly by
`ai-python` when no explicit provider config is set. Tune any setting live with
`/set <key> <value>` (and list them with `/show`); convert a legacy
`config.toml` once with `js --migrate-config`. Files of commands run
with `/load <file>`; any REPL command works there without the `/`, including
typed event hooks (`on <event> <handler>`) and aliases (`alias <name>
<command>`). `/save` writes settings, handlers and aliases back to `jsrc`.

Everything js keeps outside a project lives in `~/.js/`: `jsrc`, `logins/`,
saved sessions at `sessions/<agent_id>/<session>.jsonl` (each agent has
isolated session state), global prompt-directory agents in `agents/`, skills in
`skills/`, and per-agent runtime state in `state/`. On first start js moves its
old XDG config and data directories in; `just migrate-home` previews that. Session memory is append-only JSONL
with control marks; see the compaction section for compaction commands.

## Provider Management

`js --login` opens saved providers with their name, source, base URL, masked key,
headers, and cached model count. Select a provider to update its URL/key/headers,
manage models, or remove it. Updates save locally without fetching models or
running a generation test. Enter keeps an existing field; `-` clears it. Header
input replaces the header map (`Name=value,Other=value`) and is hidden like keys.

`<add custom provider>` is the first row; `<add registry provider>` opens the
registry login flow. In provider and model menus, `/` starts a case-insensitive
filter; Enter finishes typing, then arrows and Enter select a result. Escape
while typing clears the filter. In model checklists, Space toggles a model and
`a` / `n` select / deselect the matching rows while preserving hidden selections.
Enter saves the selection to the cache used by `--list-models` and `/model`.
The Models menu also adds ids to an empty cache or re-fetches the live list;
failed or cancelled fetches preserve the existing cache. Removed model ids can
be added again or recovered by re-fetching.

One-shot forms remain available: `js --login <id>`, `js --logout <id>`, and
`js --models-edit <id>` (offline cached-model curation).

## Compaction

`/compact [focus]`, `/compact -m <model> [focus]`, `/compact up to here`, and
`js --compact <session>` append compaction marks to JSONL instead of rewriting
history.

## Provider Compatibility

When the actual model string contains `claude`, only provider-facing tool schema
names are adapted for Claude; session history stays canonical lowercase.

## Verification

Offline suite:

```bash
python -m pytest -m "not ai_provider and not vision and not e2e"
```

In this agent environment, the verified command was:

```bash
python -m pytest -m "not ai_provider and not vision and not e2e" -p no:cacheprovider
```

Live tests marked `ai_provider`, `e2e`, or `vision` need configured provider
credentials or a local OpenAI-compatible endpoint.
