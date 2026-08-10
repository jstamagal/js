Fetch HTTP(S) or `file://` content.

Use this for current public web pages, APIs, documentation, and intentional
local `file://` reads when the information is not already available through a
more specific tool.

Parameters:
- `url` (required): `http://`, `https://`, or `file://` URL.
- `raw` (default `false`): return HTML as source instead of readable text.
- `method` (default `GET`): HTTP method for network requests.
- `headers`: either an object of header names to values or a list of
  `"Name: value"` strings. `User-Agent: js-agent/0.1` is sent unless overridden.
- `body`: raw string request body.
- `json_body`: JSON object request body. It is UTF-8 encoded and sets
  `Content-Type: application/json` unless that header is already supplied.
- `save`: path to write the response body to disk using the session cwd/path
  resolver instead of returning the body inline.

Behavior:
- GET + HTML-to-readable-text (`raw=false`) remains the default.
- HTML anchors are returned as Markdown links. Relative targets are resolved
  against the fetched URL, so the link shown to the model can be fetched as-is.
- Text-like responses include `text/*`, JSON, XML, JavaScript, form, CSV, and
  structured `+json`/`+xml` media types.
- When an inline response exceeds `ToolContext.max_tool_result_bytes`, fetch
  returns a capped preview and writes the full readable response to the standard
  `~/oldinbox/js-tool-results/result-<digest>.txt` spill location. The result
  includes that path so the rest remains readable instead of being discarded.
- A download streams to disk and returns `SAVED_RESPONSE path=... size=...`
  instead of the response body. It is not bounded by memory, so an ISO, a model
  weight, or a multi-gigabyte archive is a normal thing to save. The size
  ceiling is `limits.max_download_bytes`, which defaults to unlimited.
- GET transfers use aria2c for eight-way segmented downloading, retries, and
  resume into a hidden partial file. The destination is replaced atomically
  only after a complete download. If aria2c is unavailable, fetch emits a
  runtime warning and uses the existing urllib path.
- Unsaved binary responses and bodies larger than the inline result budget use
  the same aria2c engine, then retain the existing descriptor or spill result.
- Requests with bodies remain ordinary HTTP request/response calls; they are
  not handed to the download engine.
- Binary responses are summarized as descriptors rather than decoded into
  mojibake.
- Image responses become the existing vision image marker when vision is
  enabled. With vision disabled, or when downloaded to disk, they return a
  concise image/download descriptor.
- Errors return `ERROR: ...` strings.

When not to use:
- Do not fetch when the needed information is already available in local files
  through the filesystem tools.
- Do not read a large file inline when you only need part of it; an inline
  response still has to fit the tool-result budget. Use `save` to move bytes,
  and the filesystem tools to read them.
