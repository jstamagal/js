# js Documentation

This directory is the complete operator and technical documentation for `js`.
Root `README.md` is only a launch pad.

## Reading Order

1. [User Guide](user-guide.md): how to run the harness day to day.
2. [Configuration And Sessions](configuration-and-sessions.md): config files and
   precedence, the full key reference, environment variables, the platformdirs
   config/data layout, session files, reset/wipe/rollback, and compaction.
3. [Inline Directives](inline-directives.md): `{{VAR}}`, `!{sub args}`, and
   ` ```!lang ` fences in system prompts, the subsystems, and the
   `--im-a-pussy` opt-out (code directives run by default).
4. [Technical Guide](technical-guide.md): module map and runtime architecture.
5. [Tool System](tool-system.md): registry, schemas, descriptions, dispatch, and
   Forge-style behavior.
6. [Tools Reference](tools-reference.md): every built-in tool and what it does.
7. [Subagents](subagents.md): `task`, direct agent tools, parallelism, isolation,
   and what is not implemented.
8. [Wiki Mode](wiki.md), [Artifact Mode](artifact.md), [Drain](drain.md):
   built-in high-level workflows.
9. [Models And Providers](models-and-providers.md): ai-python, proxies, Claude
   name handling, reasoning, output caps, and vision.
10. [Testing And Development](testing-and-development.md): test suite layout and
    verification commands.

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
