# Context Compaction Completeness Audit

Read-only audit target: `js` in `~/js`.
Reference agents checked: `~/Repos/agents/codex`, `~/Repos/agents/crush`, `~/Repos/agents/claude-code`, with focused checks in `~/Repos/agents/opencode`.

## What `js` Does Today

- Defaults and knobs: auto compaction is on, notify at 50%, trigger at 80%, force at 90%, retain a 16,384-token tail, skip unless estimated savings is at least 400 tokens, char/token estimate defaults to 4.0, summary model defaults to the active model, summary cap is 4,096 with hard cap 8,192, and there is a `compact.pre_hook` (`js/settings.py:46`, `js/settings.py:190`).
- Context window lookup is stronger than a literal setting: `js` probes local provider metadata, cached server limits, `models.dev`, and cached provider ceilings (`js/runtime.py:150`).
- Auto compaction runs after a completed turn is persisted. It reads the last provider-reported `input_tokens`, divides by the resolved context window, prints a warning at notify threshold, compacts at trigger threshold, forces at force threshold, and also forces after two consecutive `max_output_tokens` incomplete responses (`js/cli.py:931`, `js/cli.py:973`, `js/cli.py:977`). It pauses after two consecutive automatic compactions (`js/cli.py:981`).
- Manual paths exist: `/compact`, `/compact up to here`, `/compact-auto on|off`, and offline `js --compact SESSION` (`js/cli.py:1460`, `js/cli.py:2301`).
- Token estimation for compaction savings and tail selection is JSON length divided by `compact.chars_per_token` (`js/runtime.py:695`). The trigger itself uses provider-reported prompt tokens when available (`js/runtime.py:1045`, `js/cli.py:944`).
- Tail preservation walks backward by estimated char budget, then backs up across an immediately preceding assistant `tool_calls` message or a leading `tool` result so a simple OpenAI-shaped call/result pair is not split at the tail boundary (`js/runtime.py:700`).
- Summarization sends one user message containing raw JSON of messages before the retained tail plus optional focus/pre-hook guidance. The prompt requires exactly six headings: Goal, Decisions and rationale, Files and code, Commands and outcomes, Errors and fixes, Pending and next step (`js/runtime.py:632`, `js/runtime.py:760`). It uses no tools and ignores stream deltas (`js/runtime.py:775`).
- Re-entry is append-only. `compact_messages` writes a `compaction:{summary,keep_from,forced}` mark to the JSONL file, reloads, and `load_messages` projects active history as one synthetic user message `<compaction-summary>...</compaction-summary>` followed by the retained tail (`js/runtime.py:804`, `js/memory.py:126`, `js/memory.py:179`, `js/memory.py:215`).
- `js` heals orphaned assistant tool calls on load by inserting explicit synthetic tool results (`js/memory.py:68`). It also drops dangling truncated tool-call args when a provider reports an incomplete/max-output response (`js/runtime.py:1117`).

## Capability Table

| capability | `js` today | Codex | Crush | Claude Code | GAP? |
|---|---|---|---|---|---|
| Trigger timing | Post-turn only, from last prompt tokens / context window; forced after repeated max-output incomplete (`js/cli.py:931`) | Pre-sampling and mid-turn compaction before more model calls (`turn.rs:797`, `turn.rs:346`) | Stops generation when remaining tokens fall below threshold, then summarizes (`agent.go:1004`, `agent.go:1155`) | Proactive auto compact by estimated current context and reactive prompt-too-long recovery call sites (`autoCompact.ts:241`, `query.ts:1119`) | Yes |
| Complete request budgeting | Does not budget next request system/tools/user/output reserve | Tracks active tokens, scoped limits, full context window (`context_window.rs:24`) | Uses cumulative prompt+completion against model window (`agent.go:1012`) | Reserves summary/output headroom and has warning/blocking thresholds (`autoCompact.ts:28`, `autoCompact.ts:72`) | Yes |
| Model window metadata | Probes local limits/server cache/models.dev (`js/runtime.py:150`) | Backend model metadata includes context window, max window, auto-compact limit, comp hash, effective usable percent (`openai_models.rs:391`) | Uses Catwalk model context window (`agent.go:1006`) | Uses model context window and max output tokens (`autoCompact.ts:33`) | Partial |
| Preserved verbatim tail | Tail by estimated token budget with simple tool-pair backup (`js/runtime.py:700`) | Keeps selected user messages, canonical initial context, remote retained messages under budgets (`compact.rs:499`, `compact_remote_v2.rs:447`) | Keeps summary message plus messages after it (`agent.go:1636`) | Builds boundary + summary + kept messages + attachments (`compact.ts:330`) | Yes |
| Tool-call/tool-result integrity | Heals missing tool results and avoids simple adjacent split (`js/memory.py:68`, `js/runtime.py:712`) | Normalizes missing outputs, removes orphan outputs, removes counterparts when pruning (`normalize.rs:17`, `normalize.rs:144`, `history.rs:187`) | Injects synthetic orphan tool results (`agent.go:1603`) | Groups by API round and preserves API invariants (`grouping.ts:3`, `sessionMemoryCompact.ts:395`) | Yes |
| Summary prompt quality | Six-heading raw JSON summary prompt (`js/runtime.py:760`) | Handoff summary prompt, structured but concise (`prompts/templates/compact/prompt.md:1`) | Detailed "only context available" prompt with files, commands, next steps (`templates/summary.md:1`) | Very detailed compact prompt with analysis/summary structure, all user messages, current work, next step (`prompt.ts:12`, `prompt.ts:61`) | Yes |
| Summary re-entry | Synthetic user message at head, append-only mark (`js/memory.py:126`) | Replacement history with compaction item/window IDs and canonical context reinjection (`compact.rs:322`) | Summary assistant message is persisted and later cast to user at active history head (`agent.go:1331`, `agent.go:1636`) | Compact boundary + summary message + metadata + relinked kept segment (`compact.ts:330`, `compact.ts:1014`) | Partial |
| Overflow retry | Retries retryable provider errors only; no context-overflow compact/rebuild path (`js/runtime.py:1086`) | Pre/mid-turn avoids most overflow; local compact can drop oldest when compact request exceeds window | No clear overflow retry in audited path | Query path can withhold prompt-too-long and rebuild after compaction (`query.ts:1065`, `query.ts:1119`) | Yes |
| Intermediate/micro compaction | None | Remote compact paths can transform/retain compacted histories | None found | Microcompact clears old compactable tool results or uses cache edits (`microCompact.ts:253`, `microCompact.ts:469`) | Yes |
| Post-compact context restoration | No re-read/re-attach of files, plans, skills, tool schemas, MCP instructions, subagents | Reinjects canonical initial/world context at model-expected position (`compact.rs:528`) | Rebuilds from summary only plus later messages | Restores files, plan, plan mode, invoked skills, async agents, deferred tools, agent list, MCP instructions (`compact.ts:517`, `compact.ts:1398`) | Yes |
| Lifecycle/UX | Terminal print only; pre-hook only (`js/runtime.py:726`, `js/cli.py:977`) | Emits lifecycle/compaction events and warnings (`compact.rs:370`) | Summary message streams like an assistant message (`agent.go:1343`) | Progress callbacks, SDK status, pre/post hooks, keepalives, telemetry (`compact.ts:406`, `compact.ts:719`, `compact.ts:1159`) | Yes |

## Ranked Gaps

### 1. Missing pre-turn full request-budget compaction

What is missing in `js`: auto compaction only runs after a turn completes and uses the previous response's `prompt_tokens` as the fullness signal (`js/cli.py:944`, `js/cli.py:977`). It does not estimate the complete next provider request: system prompt, tools, current user message, attachments/context, retained history, and reserved output allowance.

References:
- Codex runs pre-sampling compaction before creating the normal sampling step when token status says the limit is reached (`~/Repos/agents/codex/codex-rs/core/src/session/turn.rs:797`).
- Codex computes active context tokens, scoped auto-compact tokens, full-window limit, and tokens until compaction (`~/Repos/agents/codex/codex-rs/core/src/session/context_window.rs:24`).
- opencode's spec requires estimating the complete model-visible request before each provider turn and reserving the greater of output allowance and compaction buffer (`~/Repos/agents/opencode/specs/v2/session.md:113`); implementation compares `{system,messages,tools}` to `context - max(output, buffer)` (`~/Repos/agents/opencode/packages/core/src/session/compaction.ts:218`).
- Claude Code subtracts reserved summary/output tokens from the effective window (`~/Repos/agents/claude-code/services/compact/autoCompact.ts:28`, `~/Repos/agents/claude-code/services/compact/autoCompact.ts:72`).

Why it matters: mature agents compact before sending a request that is expected to overflow. `js` can discover pressure one turn too late, especially after a large user prompt, large tool schema set, restored instructions, or high max-output setting.

Implementation sketch: add a pre-flight budgeter before every provider call that assembles the actual request shape, estimates or uses known token counts for system/messages/tools, reserves `max_output_tokens` plus a configured buffer, and compacts before dispatch when over budget.

### 2. Missing mid-turn compaction before follow-up model calls

What is missing in `js`: compaction is outside `run_turn_async`; during a tool loop, a large tool result can push the next follow-up sampling request over context before `_maybe_auto_compact` ever runs (`js/cli.py:2453`, `js/cli.py:2500`).

References:
- Codex checks token status after a sampling response and, if the model needs follow-up or pending input exists, runs auto compaction mid-turn before continuing (`~/Repos/agents/codex/codex-rs/core/src/session/turn.rs:304`, `~/Repos/agents/codex/codex-rs/core/src/session/turn.rs:346`).
- Codex has explicit mid-turn placement rules: initial context is injected before the last user message so the compaction item stays last as expected by the model (`~/Repos/agents/codex/codex-rs/core/src/compact.rs:55`, `~/Repos/agents/codex/codex-rs/core/src/compact.rs:528`).
- opencode rebuilds the request after automatic compaction using a turn transition (`~/Repos/agents/opencode/packages/core/src/session/runner/llm.ts:152`, `~/Repos/agents/opencode/packages/core/src/session/runner/llm.ts:215`).

Why it matters: agentic sessions often grow inside one user turn. A mature harness must compact between tool execution and the next model call, not only between user turns.

Implementation sketch: move compaction eligibility into the model-call loop and run a mid-turn compact after tool results are appended but before the next model request, with placement rules that preserve the active user turn.

### 3. No provider context-overflow recovery path

What is missing in `js`: provider `ProviderAPIError` handling retries generic retryable errors, but there is no "context overflow/prompt too long -> compact -> rebuild same logical turn once" path (`js/runtime.py:1086`).

References:
- opencode explicitly requires one overflow-triggered compaction after provider rejection before durable assistant output/tool execution, then rebuilds the same logical provider turn (`~/Repos/agents/opencode/specs/v2/session.md:121`).
- opencode detects provider context overflow before assistant output starts, runs `compactAfterOverflow`, and transitions to a post-compaction retry (`~/Repos/agents/opencode/packages/core/src/session/runner/llm.ts:231`, `~/Repos/agents/opencode/packages/core/src/session/runner/llm.ts:277`).
- Claude Code's query path withholds prompt-too-long/media errors and can run reactive compaction then yield rebuilt post-compact messages (`~/Repos/agents/claude-code/query.ts:1065`, `~/Repos/agents/claude-code/query.ts:1119`).
- Claude's compaction call itself retries if the compact prompt is too long, dropping oldest API-round groups (`~/Repos/agents/claude-code/services/compact/compact.ts:450`, `~/Repos/agents/claude-code/services/compact/grouping.ts:3`).

Why it matters: estimates are always wrong sometimes. Mature systems have a reactive safety net and avoid losing the user's pending turn when the provider is the first accurate token counter.

Implementation sketch: classify provider overflow errors, only before any durable assistant/tool side effects, run forced compaction, rebuild the exact pending request from the compacted projection, and allow one retry.

### 4. Token accounting is not strong enough for mature compaction decisions

What is missing in `js`: `js` uses provider `input_tokens` for the post-turn trigger but uses char/4 JSON estimates for compaction savings and tail selection (`js/runtime.py:695`). It does not maintain a canonical "current context tokens = last server usage + estimates for new messages" counter, nor does it reserve output budget in the trigger.

References:
- Claude Code documents `tokenCountWithEstimation` as the canonical threshold function: last API response usage plus estimates for messages added since, with special handling for interleaved parallel tool results (`~/Repos/agents/claude-code/utils/tokens.ts:201`, `~/Repos/agents/claude-code/utils/tokens.ts:226`).
- Codex records server token usage, updates model context window usage, and uses server-observed prefill when available (`~/Repos/agents/codex/codex-rs/core/src/session/mod.rs:3653`, `~/Repos/agents/codex/codex-rs/core/src/state/auto_compact_window.rs:98`).
- Crush at least uses provider usage when available and only falls back to char/4 estimates when usage is zero (`~/Repos/agents/crush/internal/agent/usage_fallback.go:18`, `~/Repos/agents/crush/internal/agent/usage_fallback.go:171`).

Why it matters: char estimates are especially weak for tool schemas, structured content, images/documents, cached prefixes, reasoning blocks, and provider-specific serialization. This causes both late compaction and over-aggressive compaction.

Implementation sketch: introduce a token-state object that tracks last provider input/cache/output usage, estimates only messages added since, models tool schema/system overhead separately, and exposes one canonical threshold function.

### 5. Tool-call/tool-result boundary handling is too local

What is missing in `js`: `_safe_tail_start` protects only an immediately adjacent OpenAI-style assistant `tool_calls` message and following `tool` roles (`js/runtime.py:712`). It does not normalize all retained history, remove corresponding counterpart items when pruning, group API rounds, or handle provider-native tool/result structures.

References:
- Codex normalizes every request history by inserting missing outputs, removing orphan outputs, and stripping unsupported images (`~/Repos/agents/codex/codex-rs/core/src/context_manager/history.rs:355`, `~/Repos/agents/codex/codex-rs/core/src/context_manager/normalize.rs:17`, `~/Repos/agents/codex/codex-rs/core/src/context_manager/normalize.rs:144`).
- Codex removes a corresponding call/output counterpart when pruning the first item (`~/Repos/agents/codex/codex-rs/core/src/context_manager/history.rs:187`, `~/Repos/agents/codex/codex-rs/core/src/context_manager/normalize.rs:219`).
- Claude Code groups messages at API-round boundaries because tool-use validity follows the assistant response boundary, and repaired malformed dangling calls happen at API time (`~/Repos/agents/claude-code/services/compact/grouping.ts:3`).
- opencode notes provider-executed tool results may require provider-aware pruning because exact structured round trips can be required (`~/Repos/agents/opencode/CONTEXT.md:199`).

Why it matters: splitting tool pairs is one of the fastest ways to produce provider 400s or semantically corrupted replay. Simple adjacency works for one message shape, not for multi-block, interleaved, provider-native, or compacted histories.

Implementation sketch: add a provider-normalized conversation representation with call IDs, output IDs, API-round groups, and prune operations that preserve or remove complete pairs.

### 6. Summary re-entry is not a first-class compacted history boundary

What is missing in `js`: a compaction mark projects to a synthetic user message at the head plus tail (`js/memory.py:126`, `js/memory.py:179`). There is no explicit compact boundary event, compact window ID, active context epoch, preserved segment metadata, or canonical re-injection point for system/project/tool context.

References:
- Codex replaces compacted history with a `CompactedItem`, window number/IDs, replacement history, and recomputed token usage (`~/Repos/agents/codex/codex-rs/core/src/compact.rs:322`).
- Codex tracks auto-compact window IDs and server/estimated prefill baselines (`~/Repos/agents/codex/codex-rs/core/src/state/auto_compact_window.rs:33`, `~/Repos/agents/codex/codex-rs/core/src/state/auto_compact_window.rs:75`).
- Claude Code builds post-compact messages from a compact boundary marker, summary messages, kept messages, attachments, and hook results (`~/Repos/agents/claude-code/services/compact/compact.ts:330`). It annotates the boundary with preserved-segment relink metadata (`~/Repos/agents/claude-code/services/compact/compact.ts:340`).
- opencode keeps a latest compaction row and active history loader boundary (`~/Repos/agents/opencode/packages/core/src/session/history.ts:13`, `~/Repos/agents/opencode/packages/core/src/session/history.ts:66`).

Why it matters: without an explicit boundary object, later features have nowhere reliable to hang metadata such as summarized range, preserved tail, token counts, compaction reason, undo/replay linkage, or context epoch replacement.

Implementation sketch: keep append-only JSONL, but make compaction a typed mark/event with `started` and `ended`, compact ID/window ID, summarized span, retained span, token stats, reason, and projected active history semantics.

### 7. Post-compact restoration is absent

What is missing in `js`: after compaction, only the summary and recent tail remain. There is no restoration of recently read files, current plans, invoked skill instructions, loaded/deferred tool schemas, MCP instructions, agent listings, or async subagent state.

References:
- Claude Code stores read-file state, clears it, and creates post-compact file attachments from recently accessed files under count/token budgets (`~/Repos/agents/claude-code/services/compact/compact.ts:517`, `~/Repos/agents/claude-code/services/compact/compact.ts:1398`).
- Claude Code reattaches plan file, plan mode, invoked skill content, async agents, deferred tools, agent listings, and MCP instructions (`~/Repos/agents/claude-code/services/compact/compact.ts:545`, `~/Repos/agents/claude-code/services/compact/compact.ts:1488`, `~/Repos/agents/claude-code/services/compact/compact.ts:1562`).
- Codex reinjects canonical initial/world context at the correct placement after compaction (`~/Repos/agents/codex/codex-rs/core/src/compact.rs:336`, `~/Repos/agents/codex/codex-rs/core/src/compact.rs:528`).

Why it matters: coding agents depend on volatile context that is not necessarily in the text summary. Losing file contents, plan mode, active skills, or subagent/task state can make the next turn behave as if it woke up in the wrong workspace.

Implementation sketch: maintain small state registries for recently read files, active plans, invoked skills, loaded tool schemas/MCP instructions, and subagents; after compaction, re-inject budgeted attachment messages not already present in the retained tail.

### 8. No microcompaction or deterministic bulky-tool-result pruning

What is missing in `js`: every pressure event becomes full summarization. There is no cheaper path that clears old large tool results, keeps the most recent compactable results, or uses provider cache-edit APIs before summarizing.

References:
- Claude Code has `microcompactMessages`, with compactable tool sets and time/cached paths (`~/Repos/agents/claude-code/services/compact/microCompact.ts:40`, `~/Repos/agents/claude-code/services/compact/microCompact.ts:253`).
- Claude Code's time-based microcompact clears old tool result content while keeping recent ones and logs saved tokens (`~/Repos/agents/claude-code/services/compact/microCompact.ts:401`, `~/Repos/agents/claude-code/services/compact/microCompact.ts:469`).
- opencode treats deterministic old tool-result pruning as a separate follow-up to overflow compaction (`~/Repos/agents/opencode/specs/v2/session.md:121`), and its context notes bound oversized model tool output before durable publication (`~/Repos/agents/opencode/CONTEXT.md:189`).

Why it matters: full summarization is slow, lossy, and expensive. Many real context blowups come from old command/read/search outputs that can be deterministically shortened without asking a model to summarize the entire session.

Implementation sketch: add a pre-request "tool result thinning" pass that replaces old compactable tool outputs with explicit stubs, preserves the last N/full current-turn results, records saved tokens, and runs before full compact.

### 9. Summary prompt and output contract are weaker than mature agents

What is missing in `js`: the prompt is reasonable but generic. It embeds raw message JSON and asks for six headings (`js/runtime.py:760`). It does not explicitly require all user messages, current work, exact next action tied to the latest request, volatile state restoration cues, or a scratch/analysis phase stripped from the final summary.

References:
- Claude Code's prompt forbids tools up front and requires detailed sections for primary intent, technical concepts, files/code, errors/fixes, problem solving, all user messages, pending tasks, current work, and optional next step with recent quotes (`~/Repos/agents/claude-code/services/compact/prompt.ts:12`, `~/Repos/agents/claude-code/services/compact/prompt.ts:61`).
- Crush explicitly says the summary will be the only context available and requires current state, files/changes, technical context, strategy, and exact next steps (`~/Repos/agents/crush/internal/agent/templates/summary.md:1`).
- Codex's prompt is shorter but still frames this as a context checkpoint handoff and names critical decisions, constraints, next steps, and references (`~/Repos/agents/codex/codex-rs/prompts/templates/compact/prompt.md:1`).
- opencode's prompt updates an anchored previous summary and has a strict Markdown template with exact-file/symbol preservation rules (`~/Repos/agents/opencode/packages/core/src/session/compaction.ts:16`, `~/Repos/agents/opencode/packages/core/src/session/compaction.ts:154`).

Why it matters: summary quality is the core loss function. Mature agents bias summaries toward handoff fidelity, latest user intent, file/symbol paths, tool outcomes, and stale-detail removal.

Implementation sketch: replace the raw JSON prompt with a compact-specific handoff prompt that distinguishes full vs partial/repeated compaction, asks for exact active state, lists all non-tool user requests, and strips any analysis scratchpad before injection.

### 10. Repeated compaction does not update a structured anchored summary

What is missing in `js`: repeated compaction summarizes whatever active projection exists, including prior summary plus tail, but there is no explicit "previous summary + newly compacted context -> updated anchored summary" operation.

References:
- opencode's `buildPrompt` updates a previous anchored summary, preserving still-true details, removing stale ones, and merging new facts (`~/Repos/agents/opencode/packages/core/src/session/compaction.ts:154`).
- opencode's spec says repeated compactions update the previous structured summary with newly compacted messages (`~/Repos/agents/opencode/specs/v2/session.md:119`).
- Claude Code tracks recompaction metadata such as whether this is a recompaction in the chain and turns since previous compact (`~/Repos/agents/claude-code/services/compact/autoCompact.ts:279`, `~/Repos/agents/claude-code/services/compact/compact.ts:650`).

Why it matters: repeated summarization of summaries can accumulate stale facts, drift, and omit why older facts were removed. Anchored updates give the summarizer an explicit merge/delete contract.

Implementation sketch: persist previous summary separately in the compaction mark and, on later compaction, feed it in a dedicated `<previous-summary>` block with "preserve/remove/merge" instructions.

### 11. Compaction lifecycle and UX are too thin

What is missing in `js`: compaction prints terminal status and has a pre-hook, but lacks start/end events, progress callbacks, SDK status, keepalives for long compactions, post-compact hooks, durable failed-attempt semantics, and warning suppression/reset around the compact boundary.

References:
- Claude Code sets SDK status to `compacting`, sends progress events for hooks and compact start/end, runs pre-compact and post-compact hooks, and sends keepalives during long compaction (`~/Repos/agents/claude-code/services/compact/compact.ts:406`, `~/Repos/agents/claude-code/services/compact/compact.ts:719`, `~/Repos/agents/claude-code/services/compact/compact.ts:1159`).
- Claude Code centralizes post-compact cleanup of caches/state and intentionally preserves invoked skill content (`~/Repos/agents/claude-code/services/compact/postCompactCleanup.ts:12`).
- opencode's spec separates durable compaction started/ended events; only completed compaction projects a model-visible compaction message (`~/Repos/agents/opencode/specs/v2/session.md:117`).
- Crush streams the summary into a persisted assistant summary message, so the UI can show the operation as it happens (`~/Repos/agents/crush/internal/agent/agent.go:1331`, `~/Repos/agents/crush/internal/agent/agent.go:1343`).

Why it matters: compaction can be a long model call. If it fails or is canceled, users and resumable sessions need a clear boundary and prior history must remain active.

Implementation sketch: add typed lifecycle events/marks for compact start/end/fail, progress hooks, optional post-hook with summary, and ensure only successful `ended` marks alter active history projection.

### 12. Model-switch and compaction-compatibility handling is missing

What is missing in `js`: context window is resolved for the active model, but there is no explicit pre-turn compaction when switching to a smaller model or to a model with incompatible compaction/context semantics.

References:
- Codex model metadata includes `comp_hash`, `effective_context_window_percent`, `context_window`, and auto-compact limit (`~/Repos/agents/codex/codex-rs/protocol/src/openai_models.rs:391`, `~/Repos/agents/codex/codex-rs/protocol/src/openai_models.rs:400`).
- Codex runs pre-sampling compaction when the previous and current model compaction hashes differ or when switching down to a smaller context window and current usage would exceed the new window (`~/Repos/agents/codex/codex-rs/core/src/session/turn.rs:831`, `~/Repos/agents/codex/codex-rs/core/src/session/turn.rs:870`).

Why it matters: changing models can make a previously valid context invalid before the next request. Compatibility is not just token count; providers/models differ in how compacted history should be shaped.

Implementation sketch: persist previous turn model metadata, compare resolved context windows and a future `compaction_compat_hash`, and force pre-turn compaction before the next model call when compatibility changes.

## At Parity Or Fine

- `js` is not missing basic manual compaction. `/compact`, `/compact up to here`, `/compact-auto`, and offline `--compact` exist (`js/cli.py:1460`, `js/cli.py:2301`).
- `js` has a decent context-window discovery path. Local probe/server cache/catalog/ceiling lookup is better than a static setting (`js/runtime.py:150`).
- `js` already has append-only durability for compaction marks and does not rewrite old JSONL history (`js/memory.py:215`).
- `js` already has a simple but real tool-pair guard and orphan repair. It should be generalized, not thrown away (`js/runtime.py:712`, `js/memory.py:68`).
- `js` already has a useful forced path for repeated `max_output_tokens` incomplete responses (`js/cli.py:934`, `js/cli.py:973`).
- `js` already supports a compaction pre-hook. The missing piece is a post-hook/lifecycle contract, not hook support from scratch (`js/runtime.py:726`).
