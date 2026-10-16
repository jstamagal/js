Run one command with bash (or the shell set in `shell.program`) and return
exit code, stdout, and stderr. Under bash and zsh a pipeline fails when any
stage fails (`pipefail`), so `false | cat` reports exit 1.
Long output comes back as its head and its tail, with a marker between them
naming the bytes left out and the file that holds the whole stream, raw:

    [truncated: ... reached; stdout bytes 25000-980000 of 1004000 (955000 bytes) are not shown; the whole stdout is at /path/shell-3-….log — read it on with range {"start_byte": 25000}]

Read any part of that file with `read` and a byte range, so do not pipe through
`head` or `tail` just to shrink the output. Stdin is `/dev/null` and there is no terminal: a
command that reads input gets end-of-file, and a password or confirmation
prompt gets no answer.

A command does not have to finish before you get an answer. The call waits
`timeout` seconds (default from `shell.wait_seconds`); a command that finishes
inside that window comes back whole, and one still running comes back as a
handle with whatever it has printed so far:

    command still running after 30s (handle 3, pid 4242). ...
    HANDLE 3 RUNNING

The command is never killed by the wait. Then `action="poll", handle="3"`
returns new output and whether it is still running, `action="wait",
handle="3", timeout=N` blocks up to N more seconds, and `action="kill",
handle="3"` stops it. `handle` defaults to the most recent running job. Do a
poll before assuming a long build or test run has failed.

- Each call starts a fresh shell in the working directory, or in `cwd`: no `cd`, `export` or venv activation carries over to the next call. Set `cwd` instead of `cd`, use absolute paths, and call venv tools by full path.
- When js runs with `-C DIR`, the command runs in a jail: DIR is writable, the
  system is read-only, the home directory is empty except the tool directories
  on PATH and the paths the operator bound, `/tmp` is private, and the network
  is on.
{{#if fs_search}}
- Search with `fs_search`, not `grep`, `rg`, or `find`.
  Directory-only discovery: use `shell` with `fd --type d`.
{{/if}}
{{#unless fs_search}}
- `rg` and `fd` are installed; use them instead of `grep` and `find`.
{{/unless}}
{{#if read}}
- Read files with `read`, not `cat`, `head`, or `tail`.
{{/if}}
{{#unless read}}
- Read files with `sed -n 'A,Bp'`. Use `cat` only when you need the whole file.
{{/unless}}
{{#if patch}}
- Edit files with `patch`, not `sed` or `awk`.
{{/if}}
{{#unless patch}}
- Edit files with a short Python script that checks the exact old text and the
  match count before replacing. Never blind `sed -i`.
{{/unless}}
{{#if write}}
- Write files with `write`, not redirects or heredocs.
{{/if}}
{{#if remove}}
- Delete with `remove`, not `rm`.
{{/if}}
