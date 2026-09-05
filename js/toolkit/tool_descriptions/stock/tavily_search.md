Search the web via Tavily, which returns extracted page content.

Use this when you want to read what pages actually say without fetching each
one: research questions, comparisons, "how do I" queries. Each result carries
a chunk of the page's real text, and Tavily usually includes a one-paragraph
synthesized answer.

Parameters:
- `query` (required): the question or search terms; full questions work well.
- `max_results` (default `8`, range `1`–`20`): how many results to return.

Output: an answer line when Tavily produces one, then numbered results as
title, URL, and extracted content.

When not to use:
{{#if serper_search}}
- Use `serper_search` for exact-phrase or freshest-news keyword search.
{{/if}}
{{#if browse}}
- Use `browse` when you already have the URL and want the whole page.
{{/if}}
{{#if docs_search}}
- Use `docs_search` for library and framework documentation.
{{/if}}

Requires `TAVILY_API_KEY` in the environment; the tool reports a plain ERROR
when it is missing.
