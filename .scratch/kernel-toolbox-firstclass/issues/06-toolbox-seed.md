# 06 — Toolbox: ship at least one example tool so the load ritual has payoff

**What to build:**
On a fresh harness, `toolbox list` returns empty and `toolbox load` does nothing. The session-start ritual (load before writing code) has zero visible payoff, so agents skip it. Seed the toolbox with one or more example tools (self-contained, documented) so load demonstrates the pattern and gives a real payoff on first run.

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] Fresh harness `toolbox list` shows at least one shipped example tool
- [ ] `toolbox load` at session start execs the example cleanly into the kernel
- [ ] Example tool is self-contained, with a note explaining save/load lifecycle
