Search code structurally with ast-grep. Patterns match parsed syntax, so they
span whitespace and line breaks and never match inside comments or strings.
{{#if fs_search}}
Use `fs_search` for plain text, regex, and filenames.
{{/if}}
In a pattern `$NAME` matches one syntax node and `$$$ARGS` matches zero or
more; metavariables are uppercase, and reusing one requires equal captures.
Language is inferred from the file extension; set `lang` to override it.
Matches come back as an absolute `path:line` heading followed by anchored
source lines, `12:ab|code`.

Rewrites are two-step. `rewrite` alone is a dry run that shows a unified diff
and changes nothing; `apply=true` writes it, and is refused when the match
count exceeds `max_results`.
{{#if undo}}
An applied rewrite snapshots every changed file for `undo`.
{{/if}}
