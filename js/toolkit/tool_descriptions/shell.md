Run one command with `$SHELL -c` and return exit code, stdout, and stderr.
Output is capped and marked where it was cut, so do not pipe through `head` or
`tail` just to shrink it.

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

- Set `cwd` instead of `cd`.
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
