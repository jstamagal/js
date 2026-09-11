Version-current library documentation via Context7. Use it before writing code
against a library, framework, SDK, or API whenever exact syntax, config keys, or
a migration path matters. It resolves `library` to Context7's best match and
returns documentation snippets with source URLs. Context7 matches keywords, so a
hit is reported only when the requested name, case and separators ignored, is
the hit's project name (an id path segment or its title) or one whole word of
it, and a query made of one character repeated (`zzz`) is not a name and matches
nothing. Anything else comes back as no match rather than as an unrelated
project's docs. Set `topic`; an untargeted dump is long.
{{#if serper_search}}
`serper_search` is for issues, changelogs, and discussion around a library.
{{/if}}
