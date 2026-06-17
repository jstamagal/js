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
**execute code embedded in the prompt file** and are gated (see below).

Command execution captures stdout only; a trailing newline is stripped. A
nonzero exit, a missing interpreter/compiler, or a timeout raises an error and
aborts prompt assembly. The default per-directive timeout is 30 seconds.

## Enabling code execution

Code subsystems run only when inline-code execution is enabled:

```bash
js --dangerously-evaluate-inline-code -p "..."
# alias:
js --dangerously-evaluate-shell-commands -p "..."
```

The flag sets `JS_ALLOW_INLINE_CODE=1`, which the runtime reads into
`Config.allow_inline_code`. Without it, any `!{sh ...}`, `!{python ...}`,
` ```!c ` etc. raises an error telling you to pass the flag; `{{VAR}}`,
`!{env}`, and `!{file}` still work.

> **Security note.** With the flag on, `js` compiles and runs arbitrary code
> taken from your prompt files (the agent's `*.md` and any stacked `AGENTS.md`).
> Only enable it for prompts you wrote or fully trust. Treat it like running a
> script: a prompt directory is executable input once this flag is set.

## Single-pass / injection safety

Expansion is a **single pass**. Each directive is resolved once and its output
is never re-scanned. If a command's stdout or a file's contents happen to
contain another `!{...}` or `{{...}}`, that text is injected literally and does
not trigger further expansion. A model response, a fetched document, or a file
read at runtime can never smuggle in a directive that runs — only the static
prompt text is ever expanded, and only once.
