Run a terminal command through the configured system shell.

On Unix this uses `$SHELL -c` and falls back to `/bin/sh -c` when `SHELL` is
unset. On Windows it uses `COMSPEC /C`.

Use this for terminal operations such as builds, tests, git, package managers,
linters, formatters, dev tools, and project-specific programs that do not have a
dedicated tool.

Critical directory rule:
- Do not put `cd` in the command string.
- Use the `cwd` parameter to choose the working directory.
- `cd path && command` is redundant and violates the tool contract.

Do not use this for normal file operations:
{{#if fs_search}}
- File search: use `fs_search`, not `find`, `grep`, or `rg`.
- Content search: use `fs_search` with regex, not `grep` or `rg`.
- Directory shape: use `fs_search` or an explicit `shell` listing command when
  directory traversal is the actual request.
{{/if}}
{{#unless fs_search}}
- Content search: use `rg` (ripgrep), not `grep`. `rg` is installed; it skips
  binary and non-regular files, honors `.gitignore`, and is far faster.
- File finding: use `fd`, not `find`, to locate files by name or extension. `fd`
  is installed and safer around special files. Reach for `find`/`grep` only when
  `rg`/`fd` cannot express the query.
{{/unless}}
{{#if read}}
- File reads: use `read`, not `cat`, `head`, or `tail`.
{{/if}}
{{#if patch}}
- File edits: use `patch` or `multi_patch`, not `sed` or `awk`.
{{/if}}
{{#if write}}
- File writes: use `write`, not `echo > file` or heredocs.
{{/if}}
- Communication: respond directly, not with `echo` or `printf`.

Before commands that create files or directories:
- Verify the parent directory is the intended location.
- Prefer dedicated file tools for deterministic file writes.

Command construction:
- Always quote paths containing spaces.
- Add a concise `description` when the purpose is not obvious.
- Use `env` only for environment variable names that should be passed through.
- **Default child env is restricted to PATH, HOME, USER, LANG, LC_ALL, TERM, PWD, and SHELL.** Any other variable (API keys, tokens, custom vars) must be explicitly named in `env` to pass through.
- `timeout` (default `300` seconds): raise it for long builds/tests that legitimately run past 5 minutes.
- Use `keep_ansi=true` only when color/control output matters.

Output behavior:
- Output includes exit code, stdout, and stderr.
- stdout/stderr are capped by `ToolContext.max_bash_output_bytes`.
- Do not pipe through `head` or `tail` merely to reduce output; let the runtime
  cap it.

Multiple commands:
- If commands depend on each other, use `&&` in one shell call.
- Use `;` only when later commands should run even if earlier commands fail.
- If commands are independent, separate tool calls are clearer than one long
  shell string.
