Fetch a URL over HTTP(S) or `file://`.

HTML comes back as readable text with links as absolute Markdown links you can
fetch as-is; `raw=true` returns the source instead. An inline text response is
capped, and when it is cut the full text is written to a file whose path is in
the result. `save` streams the body to that path instead of returning it, with
no size limit and resumable, so large downloads belong there. Binary responses
come back as a descriptor; images come back as images when the model has vision.
{{#if browse}}
Use `browse` for pages that only render with JavaScript; this tool does not run
scripts.
{{/if}}
