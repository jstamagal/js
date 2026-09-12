Search code structurally with ast-grep. Patterns match parsed syntax, so they
span whitespace and line breaks and never match inside comments or strings.
{{#if fs_search}}
Use `fs_search` for plain text, regex, and filenames.
{{/if}}
In a pattern `$NAME` matches one syntax node and `$$$ARGS` matches zero or
more; metavariables are uppercase, and reusing one requires equal captures.
Language is inferred from the file extension; set `lang` to override it.
Under `lang="C"` a bare call pattern like `call($A)` matches nothing, because
tree-sitter-c parses that fragment as a type rather than a call; write it as an
expression statement (`call($A);`), or pass `lang="Cpp"` when `path` is a
single C file.
Matches come back as an absolute `path:line` heading followed by anchored
source lines, `12:ab|code`. When more matches exist than `max_results`, the
extra ones are dropped and the result says so; a pattern ast-grep warns about
reports the warning above the matches. The walk crosses mount points below
`path`, so a search rooted above a mounted share such as an NFS automount
enters that share; ast-grep has no `--one-file-system`, so name the directory
you want walked as `path`.

Rewrites are two-step. `rewrite` alone is a dry run that shows a unified diff
and changes nothing; `apply=true` writes it, and is refused when the match
count exceeds `max_results`.
{{#if undo}}
An applied rewrite snapshots every changed file for `undo`.
{{/if}}
