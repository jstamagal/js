Run one command with `$SHELL -c` and return exit code, stdout, and stderr.
Output is capped and marked where it was cut, so do not pipe through `head` or
`tail` just to shrink it.

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
