Search the compact catalog of tools and skills allowed for this agent, or load one result by stable id.

An empty `query` returns a compact index capped at 4 KiB. Search accepts intent
words, ranks token matches, and returns short descriptions capped at 8 KiB.
Use `kind` or exact `source` to narrow results. When `truncated` is true, repeat
the same filters with `offset` set to `next_offset` to browse the next page.

Call `load`
only with the exact `id` of a result whose `loadable` field is `true`.
Non-loadable MCP server, server-error, and collision results are status messages;
read them but do not load them. Loading a native tool makes its full schema
available on the next model call and later turns of this session. Loading a skill
returns its instructions and activates every allowed tool it declares. A new
session starts unloaded; current agent policy still limits what can be loaded.
Do not load and call that tool in the same response: load first, then call it.
