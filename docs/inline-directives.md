# Inline Directives In System Prompts

`js` expands a small set of directives in the assembled system prompt before it
reaches the model. This lets a prompt file pull in environment values, file
contents, and — when explicitly enabled — the output of commands and code
snippets.

Expansion happens at the persona-load chokepoint
(`js/persona.py:_expand_spec`), so it applies to the full assembled system
prompt: every `*.md` file in the agent's prompt directory plus any stacked
`AGENTS.md` / `AGENTS.local.md`. The implementation is `js/promptexpand.py`.

## Syntax

There are three forms.

### `{{NAME}}` — environment shorthand

```text
Current user is {{USER}}, home is {{HOME}}.
```

`{{NAME}}` is replaced by the value of environment variable `NAME`. An unset
variable expands to the empty string (no error). Only valid identifier names
(`[A-Za-z_][A-Za-z0-9_]*`) are treated as placeholders; anything else, such as
`{{ a + b }}`, is left untouched as a literal.

This form is always on and needs no flag — it only reads the environment.

### `!{subsystem args}` — inline directive

```text
Repo HEAD: !{sh git rev-parse --short HEAD}
Project rules: !{file .js/rules.md}
```

The token immediately after `!` names the **subsystem**; the rest, up to the
closing `}`, is the body/arguments. The whole `!{...}` is replaced by the
subsystem's output. Arguments cannot contain a `}`.

### ` ```!subsystem ` — fenced block

For multi-line bodies, use a fenced block whose info string starts with `!`:

````text
```!python
import platform
print(platform.python_version())
```
````

The fence body is fed to the subsystem and the entire fence (including the
backticks) is replaced by the subsystem's output. A fenced `!` block wins over
the inline form when both could match.

## Subsystems

| Subsystem | Kind | What it does |
| --- | --- | --- |
| `env` | read-only | `!{env NAME}` → value of env var `NAME` (unset → empty). Same as `{{NAME}}`. |
| `file` | read-only | `!{file PATH}` → contents of `PATH` (`~` expanded). Missing/unreadable file → embeds nothing (no error). |
| `sh` | code | Run the body with `sh -c`, inject stdout. |
| `bash` | code | Run the body with `bash -c`, inject stdout. |
| `python` / `py` | code | Write the body to a temp `.py`, run with `python3`, inject stdout. |
| `node` / `js` | code | Write the body to a temp `.js`, run with `node`, inject stdout. |
| `c` | code | Write the body to a temp `.c`, compile with `cc`, run the binary, inject stdout. |

`env` and `file` are **always on** — they only read. The remaining subsystems
**execute code embedded in the prompt file**; they run by default (see below
for opting out).

Command execution captures stdout only; a trailing newline is stripped. A
directive that fails to resolve — nonzero exit, missing interpreter/compiler,
timeout, unknown subsystem — is left **literal** in the prompt with a one-line
warning on stderr; it does not abort prompt assembly. (Callers of
`expand_prompt` can pass `on_error="raise"` for the strict behavior.) The
default per-directive timeout is 300 seconds; set
`limits.inline_code_timeout_s` or `JS_INLINE_CODE_TIMEOUT` to override it.

## Code execution is on by default

Code subsystems run by default (`runtime.allow_inline_code`, default `on`).
Opting out:

```bash
js --im-a-pussy -p "..."               # this run only (sets JS_ALLOW_INLINE_CODE=0)
set runtime.allow_inline_code off      # permanent, in jsrc
JS_ALLOW_INLINE_CODE=0 js -p "..."     # via environment
```

With code execution off, any `!{sh ...}`, `!{python ...}`, ` ```!c ` etc. is
left literal in the prompt (no error); `{{VAR}}`, `!{env}`, and `!{file}` still
expand.

> **Security note.** By default `js` compiles and runs arbitrary code taken
> from your prompt files (the agent's `*.md` and any stacked `AGENTS.md`).
> Treat a prompt directory as executable input: only add prompt dirs you wrote
> or fully trust, or run with `--im-a-pussy` / `runtime.allow_inline_code off`.

## Keeping a directive literal

A prompt that documents the syntax to itself needs to show a directive without
running it. A backslash immediately before any form (`\!{sh ...}`, `\{{VAR}}`,
`` \``` ``!sub) emits it verbatim minus the escape backslash — the universal
escape, and the only one for fenced blocks. An inline `!{...}` / `{{...}}`
fully wrapped in a markdown backtick code span is also left literal, backticks
and all.

## Single-pass / injection safety

Expansion is a **single pass**. Each directive is resolved once and its output
is never re-scanned. If a command's stdout or a file's contents happen to
contain another `!{...}` or `{{...}}`, that text is injected literally and does
not trigger further expansion. A model response, a fetched document, or a file
read at runtime can never smuggle in a directive that runs — only the static
prompt text is ever expanded, and only once.
