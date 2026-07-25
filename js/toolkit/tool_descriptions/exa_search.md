Semantic web search via Exa: describe the thing, not the keywords.

Use this when you know what you mean but not what it is called — "the blog
post arguing monorepos fail at 100 engineers", "libraries like htmx but for
websockets", "papers on prompt compression". Exa embeds the query and finds
pages by meaning, and each result includes a slice of page text.

Parameters:
- `query` (required): a description of what you are looking for; full natural
  sentences beat keyword strings here.
- `num` (default `8`): how many results to return.
- `text_chars` (default `1500`): how much page text to include per result.

Output: numbered results as title, URL, and page text.

When not to use:
{{#if serper_search}}
- Use `serper_search` for exact strings, names, and error messages.
{{/if}}
{{#if docs_search}}
- Use `docs_search` for library and framework documentation.
{{/if}}

Requires `EXA_API_KEY` in the environment; the tool reports a plain ERROR
when it is missing.
