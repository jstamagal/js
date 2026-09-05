Search the compact catalog of tools and skills allowed for this agent, or load one result by stable id.

Use `query`, `kind`, and `source` to narrow deterministic results. Call `load`
only with the exact `id` of a result whose `loadable` field is `true`.
Non-loadable MCP server, server-error, and collision results are status messages;
read them but do not load them. Loading a native tool makes its full schema
available on the next model call for the rest of this turn. Loading a skill
returns its instructions and activates every allowed tool it declares. A new
user turn resets all loaded entries.
