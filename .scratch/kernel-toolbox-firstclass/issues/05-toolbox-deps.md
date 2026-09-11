# 05 — Toolbox load: resolve sibling tool dependencies or lint for them at save

**What to build:**
Composite tools (ape_memory_recall calling hindsight_recall) depend on sibling toolbox files. Currently load-order and namespace coupling make these fragile. Toolbox should either (a) resolve sibling deps at load in dependency order, or (b) refuse/warn at save listing the sibling dependencies the file needs.

**Blocked by:** 04 — Toolbox save lint

**Status:** ready-for-agent

- [ ] Loading a toolbox whose file calls a sibling tool either loads the sibling first or warns at save with the dependency list
- [ ] No saved composite tool NameErrors from missing sibling at load time
- [ ] Docs describe how to structure composite tools safely (inline vs depend)
