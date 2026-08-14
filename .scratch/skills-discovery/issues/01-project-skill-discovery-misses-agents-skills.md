# Project skill discovery scans the wrong directories

Status: needs-triage
Filed: 2026-08-13
Component: `js/skills.py` (discovery), Skill tool

## Summary

The Skill tool's project layer scans `<project>/.skills/` and `<project>/skills/`.
The Agent Skills convention is `<project>/.agents/skills/` (cross-client) plus a
client-native dir. Result on this box: `skills-lock.json` synced all six ponytail
skills into `~/js/.agents/skills/` — the standard location — and the Skill tool
cannot see them. It also cannot see the 63 user-level skills in `~/.agents/skills/`.
The lock says installed; the tool finds nothing.

The package and global layers are correct and stay as they are
(`js/skills/` bundled; `~/.config/js/skills/` — the implementation guide
explicitly allows XDG config dirs and bundled scopes).

## Current behavior

`js/skills.py:169-176` — three layers:

- package: `js/skills/` (in-package) — OK
- global: `~/.config/js/skills/` via `paths.global_skills_dir()` — OK
- project: `<cwd>/.skills/` then `<cwd>/skills/` — **nonstandard, nothing else scanned**

`js/skills.py:204+` (`_skill_paths`) also accepts loose `*.md` files and
`README.md` as skills. The spec defines a skill as a **directory containing a
file named exactly `SKILL.md`**; the guide's example explicitly shows a loose
`README.md` being ignored.

## What the spec and implementation guide say

From the Agent Skills implementation guide (agentskills.io, "Adding skills
support to your agent"):

> Within each scope, consider scanning both a **client-specific directory** and
> the **`.agents/skills/` convention**:
>
> | Scope   | Path                               | Purpose                       |
> | ------- | ---------------------------------- | ----------------------------- |
> | Project | `<project>/.<your-client>/skills/` | Your client's native location |
> | Project | `<project>/.agents/skills/`        | Cross-client interoperability |
> | User    | `~/.<your-client>/skills/`         | Your client's native location |
> | User    | `~/.agents/skills/`                | Cross-client interoperability |

> Within each skills directory, look for **subdirectories containing a file
> named exactly `SKILL.md`**.

Precedence: project overrides user; within a scope pick first-or-last-found
consistently and **warn on collision**.

Validation (lenient): name mismatch with parent dir → warn, load anyway;
missing/empty description → skip and log; unparseable YAML → skip and log.

## Proposed fix

1. Project layer scans, in order: `<project>/.js/skills/` (client-native,
   matches the existing `.js/` project-config convention) then
   `<project>/.agents/skills/`. Drop `.skills/` and `skills/` (nothing on this
   box uses them; remove dead, no alias).
2. Add user layer: `~/.agents/skills/` beside the existing
   `~/.config/js/skills/`.
3. A skill is a subdirectory containing exactly `SKILL.md`. Drop loose `*.md`
   and `README.md` acceptance.
4. Warn (stderr/log) on name collisions instead of silent override.

## Secondary deltas from the guide (separate decisions, note only)

- Lenient validation rules above (warn-load vs skip-log).
- Trust gating for project-level skills from untrusted repos — likely
  irrelevant for a single-owner box; deliberate skip is fine.
- Catalog/disclosure guidance (name+description at startup, ~50-100
  tokens/skill; dedicated tool with enum-constrained names) — worth checking
  the Skill tool's disclosure against, separately.

## Evidence

- `~/js/.agents/skills/` contains: ponytail, ponytail-audit, ponytail-debt,
  ponytail-gain, ponytail-help, ponytail-review — all six from
  `skills-lock.json`, invisible to the Skill tool.
- `~/.agents/skills/` contains 63 skill dirs, invisible to the Skill tool.
- `~/js/.skills/` and `~/js/skills/` do not exist.

## Sources

- https://agentskills.io/specification
- https://agentskills.io/client-implementation/adding-skills-support.md
- https://github.com/agentskills/agentskills
