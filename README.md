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
just install    # js on PATH; asks for missing keys and a default model

js
js -p "summarize this repo"
git diff | js -p "review this patch"
js --commit
js --commit /path/to/repo -p "mostly housekeeping"
```

Available built-in modes:

```bash
wiki ingest ~/wiki --unit ~/wiki/inbox/source.md
wiki flow ~/wiki "ingest inbox"
wiki flow ~/wiki "ingest all inbox units"
```

## What `just install` Sets Up

`just install` puts `js` and `wiki` on PATH and installs everything js's tools
need: the kernel, vision and browser packages, js's pinned CLI binaries in
`tools/bin` (`rg`, `fd`, `bat`, `fzf`, `obscura`, `aria2c`) and the
Chromium build `browser_probe` drives. `just sync` does the same for the
development env and adds the test packages.

Two systems get less. Playwright publishes no musllinux wheels, so a musl
system goes without the `browser` extra and Chromium. A Termux install carries
the base packages and the CLI binaries `pkg` provides; `obscura` has no
verified Termux release, so the install names it and carries on.

An agent has `kernel` and `toolbox` when its agent.yaml lists them, like any
other tool. The `jev` tool needs `TYPESAFE_API_KEY`.

Python 3.12+ is required. The base packages include native extensions: core
tool validation needs `jsonschema`/`rpds-py`, and the provider SDKs need
`pydantic-core` and `jiter`. On Termux/32-bit ARM, these may need a local Rust
toolchain/build rather than an available wheel.

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
js/model_client.py                model I/O boundary over the `ai` SDK
js/runtime.py                     streaming tool-use loop
js/turn_dispatch.py               a turn's tool calls run and capped
js/toolkit/core.py                Tool, ToolContext, call_tool
js/toolkit/registry.py            registry assembly and selector filtering
js/toolkit/fs_tools.py            the file tools' table
js/toolkit/fs.py                  read/write/remove/undo
js/toolkit/patch.py               patch
js/toolkit/file_search.py         fs_search
js/toolkit/shell.py               shell
js/toolkit/fetch.py               fetch
js/toolkit/plan.py                plan
js/toolkit/skill.py               skill
js/toolkit/subagents.py           task and subagents
js/toolkit/wiki/                  deterministic tools for installed wiki agents
defaults/tool_descriptions/     model-facing tool contracts
defaults/                         stock jsrc, fset, agents, themes, help, skills and snippets
                                  each file can be shadowed in the selected home;
                                  agents also use project .js/agents/
tests/                            offline, harness, smoke, and live/proxy tests
docs/                             full user and technical documentation
```

## Tool Surface

The public registry exposes canonical tool names only; use the names documented
in [docs/tools-reference.md](docs/tools-reference.md), not compatibility
spellings like `fs_read`, `fs_write`, `cat`, `grep`, or `semantic_search`.

## Config And Model Defaults

`--home DIR` selects a complete profile; otherwise `JS_HOME` or `~/.js` is used.
The chosen directory is resolved once and created if needed. Config, agents,
skills, sessions, logs, state, cache and scratch data stay within that profile.
Project `.js/` layers still apply in the working directory.

Config is a script: each line of a `jsrc` file is a `set <key> <value>` command,
applied at startup. Files layer lowest-to-highest as `defaults/jsrc` (shipped with js: one line per setting, the built-in defaults), `~/.js/jsrc`,
then each project `.js/jsrc` and `.js/jsrc.local` from the repository root down
to the working directory; env vars override files and
CLI `--extra key=value` overrides env. js never writes `~/.js/jsrc` on its
own: it exists once you write it or run `/save`, and holds only what you set.
`js/jsrc.template` lists every setting, described, at its stock value.
`defaults/jsrc` sets `MODEL` to `cpa/claude-opus-5-5` and `EFFORT` to `medium`;
`JS_MODEL` overrides it. Explicit
`set PROVIDER`, `BASE_URL` and `API_KEY` are opt-in only; `JS_PROVIDER`, `JS_BASE_URL`,
and `JS_API_KEY` are env overrides. A model runs through a saved login, `PROVIDER`
or `BASE_URL`; with none of them it is refused. A provider's own env vars
(`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`, ...) fill its key and
base URL when nothing else sets them. Tune any setting live with
`/set <key> <value>` (and list them with `/show`); convert a legacy
`config.toml` once with `js --migrate-config`. Files of commands run
with `/load <file>`; any REPL command works there without the `/`, including
typed event hooks (`on <event> <handler>`) and aliases (`alias <name>
<command>`). `/save` writes what you changed this session into `~/.js/jsrc`
in place: the settings you `/set`, the model you picked, the formats you
`/fset`, and new handlers and aliases. A setting you set back to stock loses
its line. Nothing from the environment, a login or the defaults goes in, every
other line is left alone, and no backup copy is made.

Everything js keeps outside a project lives in `~/.js/`: `jsrc`, `logins/`,
saved sessions at `sessions/<project>/<session>.jsonl`, filed by the
project root (else the directory js started in), each with a readable `.txt` transcript beside it,
global prompt-directory agents in `agents/`, skills in
`skills/`, and per-agent runtime state in `state/`. On first start js moves its
old XDG config and data directories in; `just migrate-home` previews that. Session memory is append-only JSONL
with control marks; see the compaction section for compaction commands.

## Setup Layers And Profiles

Stock files live under `defaults/`. Copy a file to the same relative path in
`~/.js/` to change it: `agents/defaultagent/01-prompt.md` changes that prompt
while the stock manifest and environment still load. An empty file shadows the
stock file. Names in listings are the union of the layers. Skills replace a
whole directory by name. Project `.js/` overrides agents, skills, commands,
toolbox and jsrc; themes, tool descriptions, help and snippets use stock and home.
`jsrc` files layer settings, and `defaults/fset` loads before jsrc format entries.

```bash
js --home ~/profiles/review -p "review this change"
JS_HOME=~/profiles/chat js --chat -p "hello"
js --list-agents
js --list-models --json
js --list-sessions --json
js --list-providers --json
js --list-logins --json
js --verify --json
js -p "summarize this repo" --json
js -p "summarize this repo" --json-stream
```

`--home` beats `JS_HOME`, which beats `~/.js`. The directory is created when
missing; all configuration, credentials, sessions, logs, state, cache and tmp
use that home. Prompts can use `{{JS_HOME}}` or `%%JS_HOME%%`; shell commands
receive `$JS_HOME`. A missing named agent, theme or snippet is an error naming
the search directories.

`--json` writes one final object; `--json-stream` writes typed events while a run
executes. They are mutually exclusive. Models and agents have text and JSON
listings; sessions, providers and logins require `--json`.

Sampling flags override `--extra`, agent sampling and jsrc: `--temperature`,
`--top-p`, `--top-k`, `--min-p`, `--top-n-sigma`, `--repetition-penalty`,
`--presence-penalty`, `--xtc-probability`, `--xtc-threshold`, `--dry-multiplier`,
`--dry-base`, `--dry-allowed-length`, `--dry-penalty-last-n` and
`--dry-sequence-breakers`. For example, `js -p "hello" --temperature 0.7 --top-k 40`.
The Continue line carries the effective model, effort, samplers, profile and
prompt/tool switches. A resumed chat session keeps chat mode.

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

## Verification

Offline suite:

```bash
just test
```

Live tests marked `ai_provider`, `e2e`, or `vision` need configured provider
credentials or a local OpenAI-compatible endpoint: `just test-live`,
`just test-vision`.
