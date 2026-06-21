# OPEN ITEMS — what we still gotta go over

This is the agenda: topics raised in the review packet that you have NOT yet given a
decision on. TODO.md and TODO2.md hold the things we already DECIDED; this file holds
what's LEFT. As soon as we settle one, it moves into TODO2.md and comes off this list.

Reconstructed 2026-06-21 from the session log + the recovered review packet
(`review/js-recovery-review-packet.md`). One item at a time.

---

## O0 — THE BIG ONE: full module-by-module decision ledger (never started)
You asked to walk EVERY baked-in design decision in the code, module by module — not
just the bugs — each explained plainly, you approve / change / reject, agreed items go to
TODO2 or get fixed. Special focus: every "code that modifies code/tools/prompts/runtime"
feature surfaced as an explicit decision. The agent admitted this full audit was never begun.

## Safety / runtime findings with no decision yet
- O7 (finding #2) — ✅ DECIDED → TODO2. Fail closed: `cd` into a vault or pass `--vault`, else stop with an error.
  No silent fallback to `creative`. Inference stays PURPOSE.md / `wiki-*` only.
- O5 (finding #11) — ✅ DECIDED → TODO2. `runtime.py` must copy `cfg.artifact_dir/url/bin` onto the tool context;
  precedence config > `ARTIFACT_*` env > built-in default.
- O6 (finding #12) — ✅ DECIDED → TODO2. Wire `[wiki.aliases]` from config (it's documented but ignored); keep `creative`/`general` as overridable built-ins.
- O2 (finding #13) — ✅ DONE + tested. `_canonical_tool_args` now only preserves raw bytes when `json.loads` yields a dict;
  double-encoded strings fall to `_repair_jsonish` so history matches execution. Regression test added (10/10 green).
  This is the `fix/tool-args-repair-asymmetry` namesake / local-model tool-call pain.
- O3 (finding #9) — `js/toolkit/meta.py` AGENTS-file prepending drops prompt frontmatter fields
  (worker model/sampling/secondary_model lost when AGENTS files are present).
- O4 (finding #10) — locked subagent-model flag not propagated to nested worker registries
  (nested workers still see `model` in task schema even when the parent lock says no).
- O10 (finding #8) — per-provider sampling gate/translate: only partly covered by the
  "default = send nothing" decision; the actual per-wire-shape filtering work is still undetailed.

## Commit-bot bugs never discussed (talk only covered sampling + inlining)
- O1 (finding #4) — ✅ DECIDED → TODO2 (under "js --commit must just work"). Show staged AND unstaged per file, always.
- O8 (finding #14) — ✅ DECIDED → TODO2. Untracked + spec != `all` errors clearly; never silently stage whole file.
- O9 (finding #15) — ✅ DECIDED → TODO2. `_git` honors `check`; survey never reports a git failure as a clean tree.
- All three fold into the requirement: `js --commit` just works honestly in any directory, repo or not. ✅ Non-repo = AUTO `git init` + set `origin` (init-only) from config-composed URL (`git_host`/`git_user`/`git_token` set-knobs, repo=dir name); existing repos' remotes left alone.

## Config-surface drift — the specifics (general "every knob visible" was decided as D12, but these were never enumerated/decided)
- O11 — `[subagents] prefer_inherit` and `lock_model` consumed by code but missing from
  `_KNOWN_KEYS`, generated template, config.example, and config docs.
- O12 — `compact.auto` and `compact.model` consumed but missing from `_KNOWN_KEYS`/template.
- O13 — `limits.jsonl_max_line_chars` / `JS_JSONL_MAX_LINE_CHARS` in code but missing from docs/config.example.
- O14 — naming: docs/changelog use BOTH `lock_subagent_model` and TOML `lock_model`; pick ONE public key.
- O15 — README/docs link to missing `docs/agents-and-prompts.md` and
  `docs/porting-forge-tool-system-to-python.md` (restore or remove the links).
- O16 — `docs/user-guide.md` flippant default-model wording ("what I feel like it this week") —
  replace with the actual default or remove.
- O17 — docs mention a nonexistent `autocoder` example; real prompt roots are only
  `defaultagent` and `commit`.

## Prompt doctrine — flagged "owner review", never reviewed with you
- O18 — `prompts/commit/01-prompt.md` pink-elephant rewrite into a positive procedure.
- O19 — `handoff/HANDOFF.md` stores interpersonal incidents as doctrine; archive/delete decision deferred.

## Remaining real gap from scratch triage
- O20 — top-level `--agent <name>` does NOT apply that agent's frontmatter `model`
  (the personal-defaultagent override handled SELECTION, not this); prompt files don't populate
  the new model/sampling frontmatter; no per-task model pinning. The one genuinely-real leftover
  from the old HANDOFF.md NOT-DONE list.

## Deferred / confirm
- O21 — install your ChatGPT-5.5-Pro defaultagent into your PERSONAL config dir
  (e.g. `~/.config/js/agents/defaultagent/`) so it overrides the repo default. NOTE: you later
  installed your new agent as the REPO default instead, so this may be moot — confirm.
- O22 — lost history: the full vanished 2-day commit stack pre-2026-06-16 was NOT found in the
  object DB; `peeled/` (36 patches) is reference-only. The "REVIEW WITH OWNER" patches never
  reviewed: d10, d11, d12, d18, d22, d34.

---

## Decided but NOT yet tracked in TODO/TODO2 (should we add these?)
- D2 — the `fs_read` README fix + killing the prose-pinning test (decided in principle, never written down).
- D5 — the full sha256 read/write detail (chunk hash, append-anchor rule, `allow_changed`) is only
  lightly captured in TODO.
- D19/D20 — personal-defaultagent override + new-defaultagent review fixes are DONE in code but untracked,
  plus two flagged checks: (a) test the `{reasoning}` block in-harness so planning doesn't dump visibly;
  (b) deletion-wording vs `remove` (resolved by the auto-trash decision).
