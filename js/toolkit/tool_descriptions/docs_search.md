Fetch current library documentation via Context7.

Use this before writing code against any library, framework, SDK, or API —
the docs are version-current, so it beats guessing from memory whenever the
exact syntax, config keys, or migration path matters. It resolves the library
name to Context7's best match, then returns real documentation snippets with
source URLs.

Parameters:
- `library` (required): the library name as commonly written, e.g. `fastapi`,
  `next.js`, `crewai`.
- `topic`: narrow the returned docs to one area, e.g. `middleware`,
  `routing`, `authentication`. Strongly recommended — untargeted dumps are
  long.
- `tokens` (default `4000`): rough size budget for the returned docs.

Output: a header naming the matched library id, then documentation snippets,
each with its source URL.

When not to use:
{{#if browse}}
- Use `browse` when you want one specific documentation page you already have
  the URL for.
{{/if}}
{{#if serper_search}}
- Use `serper_search` for issues, changelogs, and discussion around a
  library rather than its documentation.
{{/if}}

`CONTEXT7_API_KEY` in the environment raises rate limits; without it the tool
still works at anonymous rates.
