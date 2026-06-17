# js Documentation

This directory is the complete operator and technical documentation for `js`.
Root `README.md` is only a launch pad.

## Reading Order

1. [User Guide](user-guide.md): how to run the harness day to day.
2. [Configuration And Sessions](configuration-and-sessions.md): config files and
   precedence, the full key reference, environment variables, the platformdirs
   config/data layout, session files, reset/wipe/rollback, and compaction.
   See also the annotated [config.example.toml](config.example.toml).
3. [Agents And Prompt Directories](agents-and-prompts.md): how to create global
   and project agents, id rules, reserved names, the `00-*` zero file, and
   `tools:` tool-surface frontmatter.
4. [Inline Directives](inline-directives.md): `{{VAR}}`, `!{sub args}`, and
   ` ```!lang ` fences in system prompts, the subsystems, and the
   `--dangerously-evaluate-inline-code` flag.
5. [Technical Guide](technical-guide.md): module map and runtime architecture.
6. [Tool System](tool-system.md): registry, schemas, descriptions, dispatch, and
   Forge-style behavior.
7. [Tools Reference](tools-reference.md): every built-in tool and what it does.
8. [Subagents](subagents.md): `task`, direct agent tools, parallelism, isolation,
   and what is not implemented.
9. [Wiki Mode](wiki.md), [Artifact Mode](artifact.md), [Drain](drain.md):
   built-in high-level workflows.
10. [Models And Providers](models-and-providers.md): ai-python, proxies, Claude
    name handling, reasoning, output caps, and vision.
11. [Testing And Development](testing-and-development.md): test suite layout and
    verification commands.
12. [Porting The Forge Tool System To Python](porting-forge-tool-system-to-python.md):
    checklist and architecture notes for copying the behavior into another
    Python project.

## What This Project Is

`js` is a single-user terminal LLM harness for a technical power user. It is not
a general SaaS product, and it does not optimize for beginner UX or backward
compatibility. The harness optimizes for:

- Low-friction terminal workflows.
- Explicit knobs instead of hidden policy.
- Zsh-friendly shell execution through `$SHELL`.
- Rich model-facing tool descriptions.
- Prompt-directory agents.
- Parallel subagent delegation.
- Durable JSONL sessions.
- Built-in wiki and artifact maintenance modes.

## What This Project Is Not

- It is not a web app.
- It is not a generic MCP host.
- It is not a long-term compatibility layer for old tool names.
- It has append-only summarizing compaction (`/compact`, `js --compact`) with cache-usage accounting.
- It does not currently have subagent progress handles, cancellation handles, or
  per-task model/endpoint override tools.
