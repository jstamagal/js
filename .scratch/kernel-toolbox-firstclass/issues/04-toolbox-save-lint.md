# 04 — Toolbox save: refuse (or auto-inline) functions with external namespace refs, with a clear error

**What to build:**
Saving a function that references session globals (module-level constants, helpers defined in the kernel) currently warns AFTER saving and produces a file that NameErrors on later load. Save must hard-fail (or auto-inline the referenced values) and report exactly which names are external.

**Blocked by:** 01 — Kernel preflight

**Status:** ready-for-agent

- [ ] Saving a function that references a session global fails with a list of the offending names
- [ ] No saved file can be created that would NameError on load due to namespace refs
- [ ] Auto-inline mode (if implemented) produces a self-contained file with the values inlined
