# 03 — Kernel tool: document the async pattern and guard against sync-client deadlocks

**What to build:**
The kernel runs a persistent asyncio loop. Async clients (hindsight-client, etc.) work ONLY as async def + await in cells. The client's sync wrappers that call loop.run_until_complete hang under nest_asyncio, and run_coroutine_threadsafe futures can wedge the loop permanently until kernel restart. The tool description must document the working pattern, and the harness should warn (or auto-detect) when a call risks wedging the loop.

**Blocked by:** 01 — Kernel preflight

**Status:** ready-for-agent

- [ ] Tool description documents: use async def + await in cells; sync wrappers that run_until_complete will hang
- [ ] Docs warn that run_coroutine_threadsafe can wedge the loop and the only cure is kernel restart
- [ ] A follow-up session reproduces the clean pattern (await) and it works without loop patching
- [ ] Optional: harness emits a RuntimeWarning when a sync client method is invoked in the kernel
