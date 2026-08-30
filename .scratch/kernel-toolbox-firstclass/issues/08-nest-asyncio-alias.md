# 08 — Kernel env: provide nest_asyncio import alias or document the fork name

**What to build:**
The harness ships nest-asyncio2 (a fork) but the canonical import name is nest_asyncio. Agents reaching for the documented name get ModuleNotFoundError. Either install an alias so `import nest_asyncio` works, or document the fork name prominently in the kernel tool description.

**Blocked by:** 01 — Kernel preflight

**Status:** ready-for-agent

- [ ] `import nest_asyncio` succeeds in the kernel env (alias), OR
- [ ] Kernel tool description states the fork import is `import nest_asyncio2`
- [ ] A follow-up session writing the documented import runs without ModuleNotFoundError
