Open and inspect a visual web deliverable through a real Chromium browser.

Use this when pixels and interaction matter: screenshots, canvas apps, WebGL,
landing-state defects, controls that appear only after JavaScript, or checking
whether documented input visibly changes the result. It enables SwiftShader so
WebGL can render without a physical GPU. Local HTML files and directories are
served through a temporary localhost server.

Parameters:
- `target` (required): an HTTP(S) URL or local HTML file/directory.
- `click`: optional `>`-separated chain of case-insensitive regular expressions
  matched against visible button or link text, for example `maze>play`.
- `press`: optional keyboard key to hold after clicks, such as `w` or `ArrowUp`.
- `output_dir`: optional parent directory for frames. Every run creates a unique
  probe directory beneath it.
- `settle_ms` (default `1200`): wait after loading and each click.
- `hold_ms` (default `1600`): duration to hold `press`.
- `viewport_width` and `viewport_height` (defaults `1280` by `800`).

The result reports PNG frame paths, frame dimensions, dominant colour share,
quantized unique-colour count, changed-pixel percentage after each interaction,
WebGL availability/renderer, console errors, and uncaught page errors. It crops
to the largest visible canvas when one is substantial; otherwise it captures
the page. Measurements describe the frames but do not decide whether they are
correct.

{{#if read}}
Use `read` on returned PNG paths to inspect the actual frames with vision.
{{/if}}

{{#if browse}}
When not to use: use `browse` when you only need rendered page text or links. It
is lighter and faster. Use this tool when you need pixels, input, screenshots,
or WebGL.
{{/if}}
