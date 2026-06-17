Fetch HTTP or HTTPS text content.

Use this for current public web pages, APIs, and documentation that are not
already present on disk.

Behavior:
- HTML is converted to readable text unless `raw=true`.
- Text-like responses include HTML, JSON, XML, and plain text.
- Large responses are capped by `ToolContext.max_tool_result_bytes`.
- Network errors return tool errors rather than partial success.

When not to use:
- Do not use this for binary downloads such as archives, images, audio, video,
  package files, or disk images.
- For binary downloads, use `shell` with an explicit command such as
  `curl -fLo <output_file> <url>` only when the operator's task requires it.
- Do not use web fetches when the needed information is already in local files.
