# 07 — Toolbox docs: show client cleanup (aclose in finally) for async tools

**What to build:**
Async tools that open aiohttp clients leak ClientSession/connector warnings on every call unless the client is closed. Docs (or the example seed tool) must show the pattern: construct client inside the function, wrap call in try/finally, await client.aclose().

**Blocked by:** 03 — Kernel async guard

**Status:** ready-for-agent

- [ ] Docs or seed example show client.aclose() in a finally block
- [ ] Running a saved async tool produces no 'Unclosed client session' warnings on stderr
