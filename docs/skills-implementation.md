# Skills: implementation guide

Sources: the Agent Skills client guide (`agentskills.io/client-implementation/adding-skills-support`)
and Claude Code's source (`~/vader/Repos/agents/claude-code`, `tools/SkillTool/`,
`skills/loadSkillsDir.ts`, `utils/attachments.ts`). Claude Code is cited as the
reference implementation, not as the target.

## What js does today

| Stage | Spec | js now |
|---|---|---|
| Discover | `<root>/<name>/SKILL.md` across project + user roots | Same. Layers, lowest first: built-in `defaults/skills/` (ships in the package), global `~/.agents/skills` then `~/.js/skills`, project `./.agents/skills` then `./.js/skills`. A project dir that resolves to a global dir (js run from `$HOME`) is skipped, so those skills keep the `global` source and are scanned once. Native wins within a layer; a higher layer shadows a lower one by name. |
| Parse | frontmatter `name`, `description`; body kept on disk | Same, plus `tools`, `disable-model-invocation` and `paths`. `js/skills.py` indexes metadata only; bodies read at activation. |
| Disclose | name + description of every skill visible to the model at session start | The `skill` tool includes an `<available_skills>` block and a name enum when they fit within 8 KiB. Larger catalogs use `tool_discovery` for descriptions. Built per turn after applying `SKILLS_DISABLED`, user-only skills and agent tool policy. No loadable skill: no block and no `skill` tool. |
| Activate | dedicated tool or file read; returns body, strips frontmatter | Model: `skill(name)` tool and `tool_discovery load=skill:<name>`; user: `/skill <name> [request]`. All three return one `<skill_content name="...">` block: body, `Skill directory: <dir>` with the relative-path rule, `<skill_resources>` listing up to 50 bundled files (not read), and the declared-tool outcome. `/skill` in the REPL lists every skill, user-only ones marked. |
| Path-scoped | Claude Code `paths:` | The first `read`, `patch` or `write` of a matching file starts its result with one `<js-reminder>` offering the skill, once per session (see `docs/tools-reference.md`, `skill`). The skill stays in the catalog either way. |
| User-only | `disable-model-invocation: true` | Honored. The skill is absent from the `tool_discovery` catalog, `skill` and `load=skill:<name>` refuse it, and `/skill <name>` still loads it. |
| Disable | hide disabled skills from the catalog | `SKILLS_DISABLED` (a JSON list of names) leaves those skills out of discovery for the model and the user: absent from the `tool_discovery` catalog and path-scoped offers, refused by `skill`, `load=skill:<name>` and `/skill <name>`, not listed by `/skill`. `/skills` opens a picker that lists every skill with its source and description; space switches one on or off, enter sets the setting, `/save` keeps it. |
| Validate | `name`: lowercase, digits, single hyphens, ≤64, matches its directory; `description` ≤1024 | A name breaking the character/length rule, or not matching its directory, loads with a warning (the client guide's lenient rule). A name js cannot use (spaces, slashes) skips the skill. Descriptions are cut at 1024. A missing description is still derived from the body. |
| Protect | exempt skill content from compaction | Microcompaction never clears a tool result that starts with `<skill_content`. A summary compaction summarizes it like everything else. |

## Built-in skills

`defaults/skills/<name>/SKILL.md` ships in the package (`pyproject.toml`
package-data) and is the lowest layer, so a global or project skill with the
same name shadows it. The set is vendored from github.com/mattpocock/skills:
grilling, grill-me, wait-what, diagnosing-bugs, code-review, codebase-design,
improve-codebase-architecture, handoff, wayfinder. The copies are curated, not
tracked: each `SKILL.md` frontmatter carries a `SOURCE:` line with the upstream
URL, commit, and path, and `just skills-diff [upstream]` (default
`~/matt-skills`) prints what changed upstream in each path since that commit.
Two more are MiniMax's own prompt-writing skills, vendored whole with their
`references/`: h3-image-prompt-writing (from MiniMax-AI/MiniMax-H3-Image) and
h3-prompt-writing (from MiniMax-AI/MiniMax-H3, for H3 video). The stock
`h3-image` and `h3-video` writer agents of `js generate` embed their text.
The vendored copies leave out upstream's `agents/openai.yaml`. Local edits:
code-review and wayfinder name GitHub issues via `gh` as the tracker in place
of upstream's `/setup-matt-pocock-skills` line.

## The spec (agentskills.io)

Three tiers, loaded progressively:

1. **Catalog** — name + description per skill, at session start. ~50–100 tokens each.
2. **Instructions** — full `SKILL.md` body, when activated. Recommended under 5000 tokens.
3. **Resources** — scripts/references/assets, only when the instructions cite them.

Catalog placement: either a labeled section of the system prompt, or embedded in
the activation tool's description. Include a short instruction block saying
skills exist and how to load them. If no skills are discovered, omit the block
and do not register the tool.

Activation: model-driven by default (no harness-side keyword matching). Return
body with frontmatter stripped; wrap in an identifying tag; prepend the skill's
base directory so relative paths resolve; list bundled files without reading
them. Optionally constrain the `name` argument to an enum of real skill names.

Filtering: hide disabled/denied skills from the catalog rather than listing and
refusing at activation.

Context management: exempt skill content from compaction; deduplicate repeat
activations.

## How Claude Code does it

**Discovery roots.** `~/.claude/skills`, a managed dir, and `.claude/skills` in
every directory from cwd up to the git root (monorepo support), plus `--add-dir`
roots and plugins. It does not scan `.agents/skills`.

**Disclosure — not the system prompt.** Claude Code injects the catalog as a
`system-reminder`-wrapped *user message* on the first turn:

```
The following skills are available for use with the Skill tool:

- commit: Create a git commit - Use when the user asks to commit changes
- review-pr: ...
```

It is sent once per process. A module-scope `sentSkillNames` set records what
has been announced; later turns only announce *new* skills (a delta, e.g. after
`/reload-plugins`). On `--resume`, if the transcript already contains a listing,
the next injection is suppressed. On compaction the listing is intentionally
**not** re-sent ("~4K tokens/event for marginal benefit"). The Skill tool's own
description says "Available skills are listed in system-reminder messages" and
carries the behavioural rules (invoke before responding, never mention a skill
without calling it, don't re-invoke a running one).

**Budget.** `formatCommandsWithinBudget` (`tools/SkillTool/prompt.ts`):

- Budget = 1% of the model's context window in characters (`SKILL_BUDGET_CONTEXT_PERCENT = 0.01`, 4 chars/token; 8000 chars fallback). Overridable via `SLASH_COMMAND_TOOL_CHAR_BUDGET`.
- Every description is hard-capped at 250 chars before anything else.
- If the full list fits, send it as-is.
- Otherwise: bundled (first-party) skills keep full descriptions; the remaining budget is divided evenly across the rest and each description is truncated to that share.
- If the per-skill share falls under 20 chars, non-bundled skills go **names only** (`- name`), bundled keep descriptions.

All skills are always listed; only description length degrades. There is no
separate "call discovery for descriptions" fallback in the shipped path — an
experimental `DiscoverSkillsTool` / skill search exists behind a feature flag,
and when it is on the listing is restricted to bundled + MCP skills.

**Budget as published today (code.claude.com, 2026-09-11).** The source
snapshot above is older than the shipped product. Current documented behaviour
(`docs/en/skills#skill-descriptions-are-cut-short`,
`docs/en/settings-reference#skilllistingbudgetfraction`):

| Setting | Default | Notes |
|---|---|---|
| `skillListingBudgetFraction` | `0.01` (1% of context window) | settings.json, any scope. `0 < x <= 1`. |
| `SLASH_COMMAND_TOOL_CHAR_BUDGET` | unset | env var, fixed character count; overrides the fraction |
| `skillListingMaxDescChars` | `1536` chars | per-entry cap on `description` + `when_to_use` combined (was 250 in the snapshot) |
| `skillOverrides: {"name": "name-only"}` | — | list a skill with no description to free budget |
| Overflow policy | drop descriptions **least-invoked first** | not even truncation any more; the skills you use most keep full text, all names always present |
| Compaction | re-attach most recent invocation of each skill, 5 000 tokens each, 25 000 tokens combined, most-recent-first | invoked bodies only; the listing itself is not re-sent |

In chars at 1% and ~4 chars/token: 200k ctx → 8 000 chars (~2k tokens),
1M ctx → 40 000 chars (~10k tokens). Your 41 skills are ~7.7 KB name+desc, so
they fit even the 200k budget with no degradation.

The per-entry cap going from 250 → 1536 is the notable shift: they concluded
longer descriptions match better and would rather drop whole descriptions of
unused skills than clip every description evenly. Usage-ranked dropping needs
an invocation counter persisted across sessions (they surface it via
`/skill-doctor`).

**Activation.** `Skill` tool, `{skill: string, args?: string}`; `skill` is a free
string, not an enum. Unknown name → error. `disableModelInvocation: true` in
frontmatter hides the skill from the model (user can still `/name` it). Result is
`Base directory for this skill: <dir>\n\n<body with frontmatter stripped>` with
`${CLAUDE_SKILL_DIR}` and `${CLAUDE_SESSION_ID}` substituted. Skills can declare
`allowed-tools` (auto-permitted while the skill runs), `model`, and `context: fork`
(runs in a subagent with its own budget, returns a summary).

**Compaction.** Invoked skills are tracked per agent; after compaction their
bodies are re-injected, each truncated to 5000 tokens, 25 000 tokens total.

## Your spec vs Claude Code

| | js | Claude Code |
|---|---|---|
| Roots | `~/.agents/skills`, `~/.js/skills`, then `.agents/skills` and `.js/skills` in each directory from the repository root down to the cwd | `~/.claude/skills`, `.claude/skills` walking cwd→git root. No `.agents`. |
| Layout | `<root>/<name>/SKILL.md`, exact case | Same |
| Where the catalog goes | `<available_skills>` at the end of the `skill` tool description | first-turn user message in `<system-reminder>`, plus rules in the tool description |
| When | every turn, with the tool set | once per process; deltas for late-added skills; suppressed on resume |
| Byte-stable | yes (sorted by name) | yes for the initial listing (sorted, deterministic), and it is never re-sent on compaction |
| Over budget | no budget | snapshot: proportional truncation, names-only last resort, bundled never degrade. **Shipped today:** drop descriptions of least-invoked skills first; every name always listed. |
| Per-entry description cap | 1024 (the spec's maximum) | snapshot 250 chars; **shipped today 1 536** (`skillListingMaxDescChars`) |
| Way over budget (names don't fit) | no budget | not handled; all names always listed |
| Activation arg | enum of the listed names | free string (no enum) |
| Activation payload | `<skill_content>`: body, skill directory, bundled files | base-dir header + body, frontmatter stripped |

Where Claude Code's is sharper:

- **Graceful degradation.** Truncating every description to a fair share before
  dropping to names keeps the model matching on *something* for every skill. A
  hard cliff from full descriptions to bare names loses match quality all at
  once. The 250-char per-entry cap also stops one verbose skill from starving
  the rest.
- **Tiered protection.** First-party (bundled) skills never lose their
  descriptions. For js that maps to the built-in `defaults/skills/`.
- **Delta announcements.** Byte-stable initial block, then only new names later.
  You get cache stability and still see skills added mid-session.
- **Rules live in the tool description**, which is cached with the tool set,
  not repeated in prose every turn.

Where js is sharper:

- **Tool-description placement** is one of the two placements the spec names,
  and it is cached with the tool set on every provider. Claude Code's
  user-message injection is a workaround for its attachment pipeline, not a
  design goal.
- **`.agents/skills`** support is the cross-client convention the spec
  recommends; Claude Code ignores it.
- **`tool_discovery`** also searches and loads skills, beside tools and MCP.

## How js does it

The catalog goes in the `skill` tool's description, not the system prompt.
js settles the system prompt at run assembly and records it in the session
file, before the turn's tool surface exists; what the model may load
(`SKILLS_DISABLED`, `disable-model-invocation`, whether the agent has `skill`
at all) is decided on that surface (`TurnToolSurface` in
`js/toolkit/registry.py`, `with_catalog` in `js/toolkit/skill.py`). The block
is sorted by name, so it is byte-stable and caches with the tool set.

There is no listing budget. Descriptions are capped at 1024 characters by the
spec; the block grows with the number of skills.

Repeat activations are not refused: the tool description tells the model not
to reload a loaded skill, and a summary compaction may make a reload the right
call.
