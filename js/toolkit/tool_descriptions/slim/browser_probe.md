Open a page or local HTML in real Chromium and measure what is on screen.
Use it when pixels and interaction matter: screenshots, canvas, WebGL (rendered
through SwiftShader, no GPU needed), controls that only appear after JavaScript,
or whether an input visibly changes the result. Local files and directories are
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
