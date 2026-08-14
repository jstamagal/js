# 23 test files on main import symbols that only exist on unmerged branches

Status: needs-triage
Filed: 2026-08-14
Component: tests/, offline suite

## Symptom

`pytest tests/` hits 23 collection errors, e.g.
`ImportError: cannot import name 'derive_session_name' from 'js.config'`
(tests/test_agent_model_selection.py, tests/test_agent_tools_from_config_roots.py, ...).

Verified independent of today's changes (reproduces with working tree
stashed). The files arrived in 03d05e1; the implementations they import
live on the unmerged pipe/jsdriver/* branches. Tests for features that
never landed = the unmerged-branch disease biting the test suite: the
offline gate has been broken on main since that commit.

## Fix directions (pick one)

- Merge the jsdriver branches after review (the real fix; see AGENTS.md
  step 7), or
- Move the orphaned test files to the branches that own their features,
  restoring green collection on main.
