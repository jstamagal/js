# js recovery review — plain English version

This is the same review as the hard packet, but translated into "what does this do for me?" language.

> RESTORED 2026-06-21 from the session log after the original /tmp copy was lost in a crash. Verbatim.

## Tiny dictionary

- **Superseded** = "Do not use this old patch. The useful part is already in the current repo, or a newer patch did it better."
- **Discard** = "Do not use this. It describes old/wrong behavior or would make the repo worse."
- **Review with owner** = "This might be useful, but it changes how the program behaves or how agents are instructed. The owner should look at it before it becomes permanent."
- **Keep the invariant** = "Keep the actual behavior that matters, not necessarily the old wording/test/prompt that described it."
- **Do not apply wholesale** = "Do not paste this whole old patch into the repo. It contains a mix of good work, stale work, and junk. Pick out useful pieces by hand."
- **Pink elephant wording** = "A warning that accidentally teaches the model the bad idea by naming it too loudly."
- **Prose-pinning test** = "A test that fails because docs were reworded, even though the program still works."

## d01-d09, translated

### d01 — Cover layered prompt precedence
What it was trying to do: Make sure when the program loads agent instructions, it chooses the right one if the same agent exists in multiple places.
Human version: "If I have my own local agent prompt for this project, use that instead of some generic built-in one."
Verdict: Do not apply the old patch. The current repo already has the useful behavior. Keep the idea, not the old patch.

### d02 — Clarify prompt precedence in top-level docs
What it was trying to do: Explain in README/docs which prompt wins when there are multiple prompt locations.
Human version: "The project's local agent instructions should beat my global/default instructions, and those should beat the repo's shipped defaults."
Verdict: Do not apply the old patch. The useful explanation can stay, but the old patch also created brittle wording tests.
Important note: This is where agents started turning a simple fact into doctrine. The docs should say the behavior plainly; tests should not require one magic sentence.

### d03 — Document model env precedence in README
What it was trying to do: Explain which model setting wins if you set a model in multiple ways.
Human version: "If I set a model in the environment or command line, which one actually gets used?"
Verdict: Discard. It talks about `ME_MODEL`, an old env var that was removed. Keeping this would lie to you.

### d04 — Document provider extra in top-level guidance
What it was trying to do: Explain an old provider configuration escape hatch.
Human version: "Let me pass weird raw provider options through config."
Verdict: Discard. That belonged to the old LiteLLM-style setup. The current provider code does not work that way.

### d05 — Document append-only compaction in README
What it was trying to do: Explain how session compaction records summaries.
Human version: "When the program compacts a long chat, it should add a marker/summary to history instead of rewriting old history."
Verdict: Do not apply the old patch. The current docs already say the useful thing.
Useful behavior to keep: Compaction should not secretly rewrite old session logs.

### d06 — Document Claude provider name boundary
What it was trying to do: Explain special behavior for Claude-like models.
Human version: The old patch claimed: "If the model name contains `claude`, automatically change tool names/schema for Claude compatibility."
Verdict: Discard. Current code does not do magic based on the word `claude` appearing in a model name. It uses explicit configured alias profiles instead.
Why this matters: Keeping the old docs would make you expect magic behavior that is not there.

### d07 — Document canonical tool names in README
What it was trying to do: Tell people/models to use the current tool names, not old Forge-style names.
Human version: "Use `read`, not old names like `fs_read` or `cat`."
Verdict: Rewrite, don't keep as-is.
Why: The public tool name is `read`, yes. But internally the Python function is still called `fs_read`. So saying "`fs_read` is a dead legacy alias" is confusing and partly false. Your FIXME was right.
Better human wording: "The public tool name is `read`. `fs_read` may appear inside the Python code as the implementation function, but it is not a tool name the model can call."

### d08 — Document runtime layout in top-level docs
What it was trying to do: Explain where sessions, state, prompts, and config live on disk.
Human version: "Where does this program put my chats, agent state, and config files?"
Verdict: Do not apply old patch. It used old `~/.js` wording. Current code uses platformdirs-style locations.
Useful behavior to keep: Docs should plainly say where sessions/state/config go now.

### d09 — Clarify runtime state paths in CLI help
What it was trying to do: Make CLI help mention where runtime state is stored.
Human version: "When I run `js --help`, it should tell me where session/state files live."
Verdict: Already handled in current code. Do not apply old patch.

## The big picture
The old patches fall into three buckets:
### 1. Real behavior that should stay
- project-local agent prompts should win over generic defaults;
- session compaction should not rewrite old chat logs;
- provider/model routing should avoid leaking wrong keys/endpoints;
- public tool names should be clear.
### 2. Old docs/tests that describe removed behavior
- `ME_MODEL`; LiteLLM/provider.extra raw options; magic behavior based on `claude` in a model name; old `~/.js` layout wording. These should not come back.
### 3. Agent panic text that hardened into fake law
- README saying "Do not reintroduce legacy aliases..." while naming `fs_read`, even though `fs_read` is live internally;
- tests that require docs to contain one exact sentence;
- tests that ban random substrings like `migrat`;
- handoff files recording irritation/venting as permanent guidance. These should be removed or rewritten calmly.

## The practical rescue plan
1. Fix real dangerous runtime bugs first: symlink delete bug; `fetch(file://...)` bug; stale read/write guard bug; wrong provider routing for subagent model overrides.
2. Fix config truthfulness: if a setting exists, it should be in the generated config/docs; if example config is tuned personal config, stop calling it defaults.
3. Loosen/delete doc tests that police wording instead of behavior.
4. Rewrite docs in your voice: what the tool does, where files go, how to use it.
5. Only then touch prompts, because prompts are behavior. They should be reviewed by you, not blindly rewritten by agents.

## One sentence summary
A lot of the recovered patches were attempts to document or test real behavior, but agents turned many of them into brittle laws and stale warnings. Keep the useful behavior; throw away the panic wording and obsolete docs.
