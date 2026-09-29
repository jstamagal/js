Ask a language server about a source file: `diagnostics` (errors and
warnings in the file), `definition`, `references` or `hover` (type and docs) at
a position. The server is picked by extension from what is on PATH:
basedpyright or pyright for Python, rust-analyzer, gopls, and
typescript-language-server for TypeScript and JavaScript. A file no server
covers returns one ERROR line naming what was looked for.

Each call sends the file as it is on disk now, so call `diagnostics` after an
edit to check it. The first call for a project starts the server and can take
a while; later calls reuse it. A server that is still loading the project may
return nothing or empty results; call again.

A position is `line` (1-based) plus either `character` (1-based column) or
`symbol` (text on that line; the position goes on its first occurrence).
Locations come back as `path:line:column: source line`, relative to the
working directory when under it.
