# Stale old-lineage cli.py swept onto main broke js startup and 23 test modules

Status: resolved (3b67a44); follow-up decision open
Filed: 2026-08-14. First version of this ticket blamed 03d05e1 — that was
wrong twice (bad stash-test methodology) and the reporter got called on it.
This version is the verified story.

## What actually happened

An agent session left a heavily modified `js/cli.py` (+713 lines) sitting
UNCOMMITTED in the working tree, unannounced. It was old-lineage code: it
imports `js.toolkit.artifact` (exists only in the js.old lineage) and
`derive_session_name`/`js.session_catalog` (exist only on the unmerged
pipe/jsdriver/* branches). None of its dependencies exist on main.

On 2026-08-14 the owner cleared the desk with a "." commit (e025de0),
sweeping the stale file onto main. From that moment `import js.cli` raised
ImportError, js could not start, and 23 test modules failed collection.

Resolved by restoring e025de0^'s cli.py (working, JS_MODE intact): 3b67a44.
1253 tests collect clean; test_cli_prompt_mode 62/62 green.

## The disease, again

Same failure class as the unmerged-branch rot and the silent ponytail
install: an agent produced work, did not land it, did not announce it, and
the residue detonated later in the owner's hands. AGENTS.md step 7 exists
for this. A dirty working tree at session end is an unannounced landmine.

## Follow-up decision (owner's, deliberate, piecemeal — no wholesale merge)

The old-lineage features referenced by that cli.py remain available in
history (e025de0) and on branches. A straight `git merge
pipe/jsdriver/headless-cli-integration` was attempted and aborted: the
branch predates heavy rewrites of main and depends on `toolkit/artifact`,
which main deliberately never had. If any of these are wanted, port them
individually against current main:

- Session catalog with process-backed liveness (`js/session_catalog.py`)
- Caller-managed/named sessions (`derive_session_name`: agent+cwd+key)
- Offline cached-model editing (`--models-edit`)
- `-C` working-dir session test fixes; concise help; no-save feedback
- Named agent tools from configured roots

Owner's caution stands: many bugs fixed on those branches were re-fixed on
main independently; wholesale merges risk resurrecting stale versions.
