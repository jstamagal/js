Read a web page the way a browser sees it, JavaScript included.

Use this when you have a URL and want its content: docs pages, blog posts,
dashboards, single-page apps. It drives obscura, a self-contained browser
engine, so pages that build themselves in JavaScript come back with their real
content where a plain HTTP request returns an empty shell.

Parameters:
- `url` (required): the page to read. Private, loopback, and link-local IP
  literals are allowed automatically. Hostnames keep obscura's private-network
  guard even when their names resemble a private address, so
  `http://localhost.example.com/` is still blocked.
- `dump` (default `markdown`): what to return.
  - `markdown` — readable content with links kept. Relative hrefs are rewritten
    absolute against the page URL, so every link in the output is fetchable.
    In-page anchors starting with `#` are left alone.
  - `text` — plain text, no link targets.
  - `html` — the rendered DOM after scripts have run, not the served source.
  - `links` — one link per line as URL, tab, anchor text.
  - `original` — the raw HTTP response body, byte-exact, bypassing the browser
    layer. Use for JSON, CSS, JS, images, or anything that is not a document.
  - `assets` — one JSON object per line for every sub-resource the rendered page
    references: scripts, stylesheets, images, iframes, media, embeds.
  - `cookies` — the whole cookie jar as JSON, including HttpOnly cookies that
    page scripts cannot see.
- `screenshot` (optional): a `.png` path to save a picture of the settled page.
  obscura returns EITHER a picture OR a dump, never both — when you pass this,
  the result is the `SCREENSHOT path=... size=... bytes` line and nothing else,
  and `dump` is ignored. To get the page text as well, call browse a second time
  without `screenshot`.

Choosing a dump:
- Reading a page for its content: `markdown`.
- Harvesting every outbound URL to decide what to read next: `links`.
- Inspecting markup, attributes, or a specific element: `html`.
- Pulling a JSON API or downloading a file verbatim: `original`.
- Auditing what a page loads, or replaying its resources yourself: `assets`.

Limits:
- No POST, no request headers, no request body. Reads only.
- WebGL and canvas pixel operations are not supported.

{{#if fetch}}
When not to use:
- Use `fetch` for API calls that need a method, headers, or a request body —
  this tool cannot send them.
{{/if}}

{{#if browser_probe}}
- Use `browser_probe` when the question is whether clicking something changed
  what is on screen, or whether WebGL works. This tool reads text; that one
  looks.
{{/if}}

Requires the `obscura` binary on PATH; the tool reports a plain ERROR when it
is missing.
