# Skills: implementation guide

Sources: the Agent Skills client guide (`agentskills.io/client-implementation/adding-skills-support`)
and Claude Code's source (`~/vader/Repos/agents/claude-code`, `tools/SkillTool/`,
`skills/loadSkillsDir.ts`, `utils/attachments.ts`). Claude Code is cited as the
reference implementation, not as the target.

## What js does today

| Stage | Spec | js now |
|---|---|---|
| Discover | `<root>/<name>/SKILL.md` across project + user roots | Same. `~/.agents/skills`, `~/.config/js/skills`, `./.agents/skills`, `./.js/skills`, package `js/skills/`. Native wins within a layer, project over global. |
| Parse | frontmatter `name`, `description`; body kept on disk | Same. `js/skills.py` indexes metadata only; bodies read at activation. |
| **Disclose** | **name + description of every skill visible to the model at session start** | **Nothing.** Skills exist only inside the `tool_discovery` catalog. The model has to call `tool_discovery` with an empty query to learn a skill exists. There is no skills block in the system prompt or first turn. |
| Activate | dedicated tool or file read; returns body, strips frontmatter | `skill(name)` tool and `tool_discovery load=skill:<name>`. Returns body, activates declared tools. |
| Protect | exempt skill content from compaction | Not checked in this pass. |

The disclosure gap is why agents never load skills: a skill the model cannot see
is a skill it will never ask for.

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

| Knob | Default | Notes |
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

| | Yours | Claude Code |
|---|---|---|
| Roots | `~/.config/js/skills` (→ `~/.js/skills`), `~/.agents/skills`, `./.js/skills`, `./.agents/skills` | `~/.claude/skills`, `.claude/skills` walking cwd→git root. No `.agents`. |
| Layout | `<root>/<name>/SKILL.md`, exact case | Same |
| Where the catalog goes | system prompt, under a "skills available" heading | first-turn user message in `<system-reminder>`, plus rules in the tool description |
| When | once per session | once per process; deltas for late-added skills; suppressed on resume |
| Byte-stable | required | yes for the initial listing (sorted, deterministic), and it is never re-sent on compaction |
| Over budget | names only + "use discovery for descriptions" | snapshot: proportional truncation, names-only last resort, bundled never degrade. **Shipped today:** drop descriptions of least-invoked skills first; every name always listed. |
| Per-entry description cap | — | snapshot 250 chars; **shipped today 1 536** (`skillListingMaxDescChars`) |
| Way over budget (names don't fit) | undecided | not handled; all names always listed |
| Activation arg | — | free string (no enum) |
| Activation payload | — | base-dir header + body, frontmatter stripped |

Where Claude Code's is sharper:

- **Graceful degradation.** Truncating every description to a fair share before
  dropping to names keeps the model matching on *something* for every skill. A
  hard cliff from full descriptions to bare names loses match quality all at
  once. The 250-char per-entry cap also stops one verbose skill from starving
  the rest.
- **Tiered protection.** First-party (bundled) skills never lose their
  descriptions. For js that maps to package `js/skills/`.
- **Delta announcements.** Byte-stable initial block, then only new names later.
  You get cache stability and still see skills added mid-session.
- **Rules live in the tool description**, which is cached with the tool set,
  not repeated in prose every turn.

Where yours is sharper:

- **System prompt placement** is what the spec calls simplest and most portable,
  and it is cached as part of the system block on every provider. Claude Code's
  user-message injection is a workaround for its attachment pipeline, not a
  design goal.
- **`.agents/skills`** support is the cross-client convention the spec
  recommends; Claude Code ignores it.
- **Explicit lazy fallback.** js already has `tool_discovery`. "Names only, call
  `tool_discovery kind=skill` for descriptions" is a real path Claude Code lacks
  in its shipped build.

For the unanswered case (names alone exceed budget): Claude Code has no answer
either. The spec's numbers say it should not happen — 41 skills is ~600 bytes of
names. If it ever does, the lazy fallback already covers it: emit the count and
the discovery instruction, no names.

## Implementing it in js

Everything below is disclosure; discovery and activation already exist.

1. **Render a catalog block** from `SkillCatalog.skills` (already sorted,
   deterministic):

   ```
   ## Skills available
   Load one with `skill` (name) or `tool_discovery` (load=skill:<name>) when a
   task matches its description. Do not load a skill that is already active.

   - code-review: Review the changes since a fixed point ...
   - tdd: Test-driven development. Use when ...
   ```

   Description cap 1 536 chars (current Claude Code default). Budget 1% of
   the model's context window in chars, 8 000 fallback, overridable by a
   fixed char count. Over budget → package skills keep descriptions, then
   drop descriptions least-used-first if an activation counter exists,
   otherwise truncate the rest to an even share; share under ~20 chars →
   names only plus `Descriptions: tool_discovery {"kind":"skill"}`. If no
   skills, emit nothing and do not register `skill`.

2. **Place it in the system prompt** where `JS.md`/agent prompts are assembled
   (`js/cli.py` → `js/memory.py`), after the operator context. Compute it
   once when the session's `LazyToolRegistry` is built; the string is stored,
   not recomputed per turn.

3. **Move the behavioural rules into `skill.md`'s tool description** and
   reference the block: "Available skills are listed under *Skills available* in
   the system prompt."

4. **Hide filtered skills.** Anything policy-denied (`allowed.resolve("skill")`
   is None, or a skill whose declared tools are all denied) is omitted from the
   block, not listed and refused.

5. **Compaction.** Do not re-send the catalog; it is in the system prompt.
   Track activated skill names per session and skip re-injection on repeat
   `skill` calls (return "already active").

6. **Tests** (behaviour only): the block is byte-identical across two builds
   from the same catalog; a skill listed in the block is loadable by the name
   shown; a denied skill is absent from the block; with an empty catalog the
   block is absent and `skill` is not registered; over budget every name is
   still present.

Optional, from Claude Code, if wanted later: `disable-model-invocation`
frontmatter to hide a skill from the model while keeping it user-invokable;
`Base directory for this skill:` header in the activation payload so relative
paths in a skill body resolve.
