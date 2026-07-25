Read a web page the way a browser sees it, JavaScript included.

Use this when you have a URL and want its content: docs pages, blog posts,
dashboards, single-page apps. It drives obscura, a lightweight headless
browser, so pages that render through JavaScript come back with their real
content where a plain HTTP fetch returns an empty shell. It cannot run WebGL
or take screenshots — it reads, it does not look.

Parameters:
- `url` (required): the page to read. Localhost and private-network URLs are
  allowed and flagged through automatically.
- `dump` (default `markdown`): what to return — `markdown` for readable
  content, `text` for plain text, `html` for rendered source, `links` for
  the page's link list.

{{#if fetch}}
When not to use:
- Use `fetch` for APIs, JSON, file downloads, and pages you know are static —
  it is faster and does not spin up a browser.
{{/if}}

Requires the `obscura` binary on PATH; the tool reports a plain ERROR when it
is missing.
