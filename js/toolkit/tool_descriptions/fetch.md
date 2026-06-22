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
- `save` or `download`: path to write the response body to disk using the
  session cwd/path resolver instead of returning the body inline.

Behavior:
- GET + HTML-to-readable-text (`raw=false`) remains the default.
- Text-like responses include `text/*`, JSON, XML, JavaScript, form, CSV, and
  structured `+json`/`+xml` media types.
- Inline responses are capped by `ToolContext.max_tool_result_bytes` and are
  marked `[truncated]` when clipped.
- Downloads are capped at 32 MiB and return `SAVED_RESPONSE path=... size=...`
  instead of the response body.
- Binary responses are summarized as descriptors rather than decoded into
  mojibake.
- Image responses become the existing vision image marker when vision is
  enabled. With vision disabled, or when downloaded to disk, they return a
  concise image/download descriptor.
- Errors return `ERROR: ...` strings.

When not to use:
- Do not fetch when the needed information is already available in local files
  through the filesystem tools.
- Do not use this as a general-purpose large artifact downloader; the download
  guard is intentionally 32 MiB.
