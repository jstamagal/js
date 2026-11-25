Web search via Tavily. Each result carries extracted page text, usually with a
one-paragraph synthesized answer first, so research and "how do I" questions
get read without fetching each page.
{{#if serper_search}}
`serper_search` is for exact phrases and the freshest news.
{{/if}}
{{#if exa_search}}
`exa_search` finds things you can describe but not name.
{{/if}}
{{#if docs_search}}
`docs_search` is for library documentation.
{{/if}}
