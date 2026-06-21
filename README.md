# js

`js` is a personal terminal LLM harness written in Python. It runs interactive
chat, one-shot prompts, pipe workflows, Forge-style local tools, parallel
subagents, wiki maintenance, artifact curation, drain jobs, and commit-agent
workflows through the Vercel AI Python SDK (`ai-python`).
This repo is for a power user, not a product team. The design bias is low
friction with lots of knobs: direct shell access, explicit sessions, prompt
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
js --wiki=ingest --vault=creative ~/notes/source.md
js --wiki=ingest,synthesize --vault=general ~/papers/paper.pdf
js --artifact=curate
js --artifact=query "find the last handoff"
js-drain creative -a
```

## Documentation

Start here:

- [docs/README.md](docs/README.md): documentation map.
- [docs/user-guide.md](docs/user-guide.md): commands, workflows, and daily use.
- [docs/technical-guide.md](docs/technical-guide.md): architecture and runtime internals.
- [docs/tool-system.md](docs/tool-system.md): registry, schemas, dispatch, descriptions, and Forge parity notes.
- [docs/tools-reference.md](docs/tools-reference.md): all public tools.
- [docs/subagents.md](docs/subagents.md): `task`, generated agent tools, isolation, and limits.
- [docs/agents-and-prompts.md](docs/agents-and-prompts.md): create global/project agents, id rules, reserved names, `tools:` frontmatter.
- [docs/inline-directives.md](docs/inline-directives.md): `{{VAR}}` / `!{sub}` / `` ```!lang `` expansion and the inline-code flag.
- [docs/configuration-and-sessions.md](docs/configuration-and-sessions.md): config precedence, full key reference, env vars, sessions, memory, and compaction.
- [docs/config.example.toml](docs/config.example.toml): annotated example config covering every supported key.
- [docs/wiki.md](docs/wiki.md), [docs/artifact.md](docs/artifact.md), [docs/drain.md](docs/drain.md): built-in modes.
- [docs/models-and-providers.md](docs/models-and-providers.md): ai-python routing, proxies, Claude naming, reasoning, vision.
- [docs/porting-forge-tool-system-to-python.md](docs/porting-forge-tool-system-to-python.md): how to port this style of tool system into another Python project.

## Project Map

```text
js/model_client.py                 single `ai` SDK import boundary
js/runtime.py                      streaming loop and tool dispatch
js/toolkit/core.py                Tool, ToolContext, call_tool
js/toolkit/registry.py            registry assembly and selector filtering
js/toolkit/fs.py                  read/write/search/patch/remove/undo
js/toolkit/process_net.py         shell and fetch
js/toolkit/meta.py                todo/followup/plan/skill/task/subagents
js/toolkit/wiki/                  wiki tools and wiki prompt builder
js/toolkit/artifact/              artifact tools and artifact prompt builder
js/toolkit/tool_descriptions/     model-facing tool contracts
prompts/                          repo prompt-directory agents; layered with platform config agents/ and project .js/agents/
                                  (repo `prompts/`, global `agents/` in the platform config dir, and project `.js/agents/`;
                                  project scope wins over global, which wins over repo)
tests/                            offline, harness, smoke, and live/proxy tests
docs/                             full user and technical documentation
```

## Tool Surface

Do not reintroduce legacy aliases such as `fs_read`, `fs_write`, `cat`, `grep`,
or `semantic_search`; use the canonical tool names documented in
[docs/tools-reference.md](docs/tools-reference.md).
FIXME: fs_read seems pretty live to me?

## Config And Model Defaults
Config files layer as the platform config `config.toml`, project `.js/config.toml`,
then project `.js/config.local.toml`; env overrides files and CLI flags/`--extra`
override env. Built-in `[model].id` defaults to `deepseek/deepseek-v4-flash`;
`JS_MODEL` overrides configi. Explicit `[provider] id/base_url/api_key`
are opt-in only; `JS_PROVIDER`, `JS_BASE_URL`, and `JS_API_KEY` are env
overrides. Official SDK env vars (`AI_GATEWAY_API_KEY`, `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`) are read directly by `ai-python` when no
explicit provider config is set.

Config, logins, and the model cache live in the platform config dir; saved
sessions live at the platform data `sessions/<agent_id>/<session>.jsonl`, and
each agent has isolated session state. Global prompt-directory agents live in
the platform config `agents/`, skills live in `skills/`, and per-agent runtime
state lives in the platform data `state/`. Session memory is append-only JSONL
with control marks; see the compaction section for compaction commands.

## Compaction

`/compact [focus]`, `/compact up to here`, and `js --compact <session>` append
compaction marks to JSONL instead of rewriting history.

## Provider Compatibility

When the actual model string contains `claude`, only provider-facing tool schema
names are adapted for Claude; session history stays canonical lowercase.

## Verification

Offline suite:

```bash
python -m pytest -m "not ai_provider and not vision"
```

In this agent environment, the verified command was:

```bash
python -m pytest -m "not ai_provider and not vision" -p no:cacheprovider
```

Live tests marked `ai_provider`, `e2e`, or `vision` need configured provider
credentials or a local OpenAI-compatible endpoint.
