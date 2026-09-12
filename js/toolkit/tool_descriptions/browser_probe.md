Render and screenshot a page or local HTML in real Chromium: pixels, canvas,
WebGL (through SwiftShader, no GPU needed), and whether an interaction changes
the result. Use it when how the page looks or responds matters, including
controls that only appear after JavaScript. Local files and directories are
served from a temporary localhost server.

`click` is a `>`-separated chain of case-insensitive regexes matched against
visible button or link text, such as `maze>play`; `press` is a key held for
`hold_ms` after the clicks.

The result lists PNG frame paths plus per-frame metrics: dimensions,
dominant-colour share, unique-colour count, changed pixels after each
interaction, WebGL availability, console and page errors. It crops to the
largest substantial canvas when there is one. Metrics describe the frames; they
do not judge them.
{{#if read}}
`read` a returned PNG to look at the frame.
{{/if}}
{{#if browse}}
`browse` is lighter when rendered text or links are all you need.
{{/if}}
