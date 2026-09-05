Search Google via the Serper API.

Use this for ordinary keyword web search: current facts, error messages,
product names, exact phrases, recent events. Results are Google's organic
hits, so it is the closest to "what a person would find".

Parameters:
- `query` (required): the search terms, phrased like a Google query.
- `num` (default `8`, range `1`–`100`): how many organic results to return.

Output: an answer-box line when Google shows one, then numbered results as
title, URL, and snippet.

When not to use:
{{#if tavily_search}}
- Use `tavily_search` when you want extracted page content and a synthesized
  answer rather than snippets.
{{/if}}
{{#if exa_search}}
- Use `exa_search` when you can only describe the thing conceptually and
  keyword search keeps missing it.
{{/if}}
{{#if docs_search}}
- Use `docs_search` for library and framework documentation.
{{/if}}

Requires `SERPER_API_KEY` in the environment; the tool reports a plain ERROR
when it is missing.
