# 01 — Kernel tool: fail loudly at session start when jupyter deps are missing, with the real recovery path

**What to build:**
When a session boots, the harness checks that the kernel's dependencies (jupyter_client, ipykernel) are importable in the kernel env. If they are missing, the session fails loudly BEFORE any agent turn — not on first kernel tool call — and the error tells the agent the real location and command: the harness pyproject/justfile at ~/js, run `just install` there. The current behavior defers the failure to first kernel use and points at ~/.config/js/pyproject.toml and `just sync`, neither of which exists.

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] A fresh box with no kernel deps shows the failure at session start, not at first kernel call
- [ ] The error message resolves to a real path (~/js/pyproject.toml) and a real recipe (just install in ~/js)
- [ ] After `just install` in ~/js, the kernel boots on the next session with no further manual setup
- [ ] The kernel tool's own error text no longer names `just sync` or a nonexistent pyproject path
