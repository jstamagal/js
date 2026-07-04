Find likely relevant local code or text by intent rather than exact spelling.

This is the default discovery tool when exploring unfamiliar behavior,
architecture, patterns, or documentation and you do not know the exact words to
search for.

Use for:
- Finding the implementation of a feature or algorithm.
- Understanding how a system works across multiple files.
- Discovering architectural patterns and design approaches.
- Locating examples, tests, fixtures, or documentation.
- Finding where technologies, libraries, or workflows are used.

When not to use:
{{#if fs_search}}
- Use `fs_search` for exact strings, TODOs, identifiers, literals, or regex.
- Use `fs_search` when you need all occurrences of a variable or function name.
{{/if}}
{{#unless fs_search}}
- This tool is not for exact strings, TODOs, identifiers, literals, regex, or all
  occurrences of a variable or function name.
{{/unless}}
{{#if read}}
- Use `read` when you already know the file path.
{{/if}}

Query guidance:
- Pass 2-3 focused query objects for broad exploration.
- Use `query` for what you are looking for.
- Use `use_case` for why you are searching; this helps rank results.
- Use optional `path`, `glob`, and `limit` to bound work.
- Avoid vague queries such as `auth`, `tools`, or `bug`; name the behavior or
  concept you need.
- If seeking docs, use doc-focused terms such as `setup guide` or
  `configuration`.
- If seeking code, use implementation terms such as `token refresh logic` or
  `error handling`.

Implementation note:
- Results are local term-ranked file:line snippets.
- No embeddings, network calls, or external index are used in this runtime.
