# js against Claude Code, Codex and pi

Survey of 2026-09-29. Four read-only reviews:
- three Sonnet 5.5 agents, each comparing one slice of js with the same slice in the other three harnesses:
  - agent loop and model I/O
  - tools
  - sessions, config and operator UX
- one architecture review of js alone.

Sources:
- Claude Code: `~/Repos/agents/claude-code`. Partial source: `REPLTool`, `snipCompact` and a few others are stubs.
- Codex: `~/Repos/agents/codex/codex-rs`.
- pi: `~/Repos/agents/pi/packages`.

Safety, sandboxing and approval flows are left out on purpose.

**How far to trust it:**
- Items marked ✔ were re-read in js's code after the survey.
- All other js citations come from the agents' own reads.
- Citations into the other three harnesses come from survey agents and were not re-checked. Spot-check before acting on one.

## At a glance

| capability | js | Claude Code | Codex | pi |
|---|---|---|---|---|
| Read-only tool calls run in parallel | yes, readers-writer, up to 8 ✔ | yes, up to 10 | yes, RwLock | yes |
| Retry honours `Retry-After` | no, 2 attempts ✔ | yes, 10 | yes | yes |
| Shell output past the cap | **head kept, tail lost** ✔ | whole stream to file | head + tail | tail + spill file |
| Stale-edit guard | per line range, returns recovery diff | per-file mtime | none | none |
| `patch` result shows the diff | yes | no | no | no |
| Fuzzy match on edit | no (nearest-line hint) | quote styles | 4 looser passes | NFKC + quotes |
| Oversized result spill | byte + line continuation named | preview + path | none | bash/MCP only |
| Long shell jobs | handle, never killed | backgrounded | session id | blocks |
| Async subagents | no | yes, notification | yes, mailbox | ? |
| Persistent code kernel | yes, live `NAMESPACE` | stub in source | fresh V8 per cell | fresh QuickJS |
| Model recorded per turn | no, start only (being fixed) | ? | yes | yes |
| Branch / fork / rewind | no (designed) | yes | yes | yes |
| Session picker with search | no (designed) | yes | yes | yes |
| Structured headless output | no | stream-json | `exec --json` | json + rpc |
| Cost accounting | no | yes | tokens only | yes |
| One config grammar (file = REPL) | yes | no | no | no |
| Prompt-time code directives | yes | no | no | no |
| Project memory walks up the tree | no | yes | yes | yes |
| Plugins | no | yes | yes | yes |
| Remappable keys | no | yes | yes | yes |

## Bets that paid off

**The system prompt is frozen into the session.** Resume sends a byte-identical prefix, and `prompt_seen:` warns when prompt files change (`memory.py:261-330`). Claude Code rebuilds its prompt every turn and needs cache-boundary markers. Codex stores base instructions but re-injects context diffs. js has the cleanest cache story of the four.

**Tool results are clipped once and stay clipped.** `_fair_share_ceiling` (`runtime.py:756`) water-fills a 200 kB per-turn budget, so the fattest result loses bytes first. The clipped text is what enters history, so the prefix never changes later. Claude Code reaches the same budget but needs a replacement-state object to keep it stable.

**Oversized results name the exact continuing read.** `spill_oversized_result` (`runtime.py:677`) returns a preview plus `start_byte` and `start_line` for `read`. Claude Code gives a path and a 2 kB preview. Codex middle-truncates.

**Token counts are anchored to the provider and calibrated.** `TokenState` anchors on the last real usage and estimates only the delta. The reply reserve is capped at 20k, like Claude Code.

**The context-overflow ladder is the most persistent of the four.**
1. Clear old results.
2. Summarise before the current message.
3. Summarise the turn and keep a tail.
4. If the provider still rejects, halve the tail, up to 3 rounds (`runtime.py:1505-1636`).

pi retries once. Codex does not recover inside a normal turn.

**Edit safety is the strictest, and cheap to recover from.**
- A stale edit is refused, and js returns the diff between what was read and what is on disk now. Coverage is tracked per line range (`fs.py:138`). A stale edit costs one turn in js and two in Claude Code. Codex and pi have no guard.
- `patch` returns the diff it wrote. None of the others show the model its own edit.
- A miss gets "closest line is N" (`fs.py:768`).
- Batch edits are all-or-nothing, and each edit sees the result of the one before it.

**Shell never kills a long job.** It hands back `HANDLE n RUNNING` with poll, wait and kill, and later reads return only new bytes. This is on par with Claude Code and Codex. pi blocks.

**Tool descriptions adapt to the tools present.** The `{{#if read}}` blocks in `tool_descriptions/*.md` mean the shell text says "use fs_search, not grep" only when `fs_search` is on the surface. None of the others do this.

**The tool policy is declarative, and you can see why each tool is on or off.** `agent.yaml` noun:modifier entries resolve against `tools.yaml` tags, and `/tools` prints the rule that decided each tool. The others decide in code.

**One grammar for config.** `set key value` is the same line in `js/jsrc`, in `/set` and in `--extra`. `/save` writes only what differs from the defaults, and `set -key` reverts a setting. The others keep their file format and their runtime settings separate.

**`JS.md` rather than a global `AGENTS.md`**, so repo-shaped instructions don't leak into every project (`paths.py:74-81`). **Single-pass prompt directives**, so a directive's output is never re-scanned (`promptexpand.py`). **Agents are prompt directories** with ordered fragments and a manifest, which is a richer unit than Claude Code's single file or pi's `SYSTEM.md`.

## Where bucking the norm looks wrong

**Shell output keeps the head and throws away the tail.** ✔ `_StreamCapture.feed` stops keeping bytes at the cap (`capped_process.py:64`). The cap is 150 kB, and the spill afterwards works from the already-clipped text. The compiler error at the end of a long build log is gone for good. The other three keep the tail or the whole stream.

**Retry gives up after two attempts and ignores `Retry-After`.** ✔ The check is `transport_retries == 2` (`runtime.py:1816`), and "retry-after" appears nowhere in js. A 429 with a 30-second `Retry-After` kills a turn in about 3 seconds. Claude Code retries 10 times, and all three honour the header.

**A direct Anthropic provider gets no thinking.** Done in js-1g1.12: every provider on the anthropic SDK now takes `reasoning` as thinking, adaptive with an effort on Claude 4.6 and later and a token budget on the rest (`reasoning.anthropic_thinking`). Before, `steers_via_effort` covered only Codex, OpenAI-SDK and implicit gateway endpoints, and a Claude model on the direct API ran with thinking off whatever `reasoning` said.

**A turn cut off by max output tokens just ends.** It drops the dangling calls and tells the user to retry (`runtime.py:1844-1858`). Claude Code escalates to 64k and then sends up to 3 resume nudges. pi fails the calls and keeps looping.

**Compaction summaries are cold, bloated, and never give up.**
- The history is sent as indented JSON with whole tool results (`compaction.py:567`), and it overflows often enough to need recursive splitting.
- A failed summary is retried every iteration, because nothing breaks the loop.
- pi serialises the history as plain text with tool results clipped to 2000 chars. Claude Code forks the summary call so it shares the main thread's cache prefix, and stops after 3 failures.

**Clearing old results busts the prompt cache.** `microcompact` rewrites mid-history results whenever the budget trips (`compaction.py:192`). Claude Code clears only after the cache TTL has already expired.

**`read` prefixes every line with `N:hash|`, and `patch` can't use it.** It costs tokens on every read line. The stale guard already catches changes without it.

**`task` blocks the parent until every child finishes.** A slow child stalls the whole turn. Claude Code and Codex return immediately and deliver the result later.

**Lazy loading costs a round trip, and its ranking is weak.** "Do not load and call that tool in the same response." Discovery ranks by token overlap (`discovery.py:97`), while all three others use BM25. Claude Code expands tool references inline, within the same response.

**`patch` has no fuzzy fallback.** A smart quote the model pasted costs a turn in js and nothing in the other three.

**Sessions are filed per agent, and the model is recorded only at start.** Resume can come back on the wrong model. Both are fixed by the design in `docs/sessions-and-home-design.md` and are being built now (js-1g1.2).

**`AGENTS.md` is read only from the current directory** (`config.py:628`). Run js from `repo/sub/` and the repo's instructions are silently lost. The other three walk up the tree, and Codex caps the total at 32 KiB.

**Two REPL loops (async and `--blocking`).** Every input feature has to land twice, or it goes missing in one of them. None of the others keeps a second loop.

## Novel in js

- **A persistent IPython kernel whose every result includes a fresh `NAMESPACE` line.** It stays accurate after compaction. Codex and pi start a fresh isolate for every cell.
- **`toolbox`: tools the model writes itself.** Each one has revisions, an author-model record and rollback, and a save is refused if the function uses names the file would not carry.
- **Chars-per-token calibrated against the provider's real count,** invalidated by a fingerprint of the message prefix. The others use a fixed 4.
- **A forensic record for every compaction attempt** (`compaction_flight.py`): inputs, request, response, commit, and the traceback if it failed.
- **Per-model tool aliases** that also rewrite the backticked names inside descriptions.
- **Tool-call batches are cleaned before dispatch:** made canonical, deduplicated, validated and capped at 50.
- **Read coverage is tracked by line range,** which is finer than Claude Code's per-file mtime.
- **One discovery catalog for tools, skills and MCP servers,** paged by bytes.
- **An MCP host that opens no connection until a scoped discovery,** so adding servers never grows the schema.
- **Argument ban patterns** that refuse a call before it runs.
- **Every agent directory becomes its own tool.**
- **`toolstats` audits shell habits:** calls to `cat`, `grep`, `find` or `sed -i` where a dedicated tool exists.
- **An ircII scripting layer:** `on <event> <command>`, `alias`, `load`, and `/save` persisting them. The hook language is the command language.
- **A vi input buffer with a real ex line** (`:w`, `:x`, `:e`, `:r`, `:n`, and `:<program>` runs the program on the buffer).
- **`set -key` reverts a setting through the layers,** and `js/jsrc` must list every setting.
- **Session leases with pid and start-time checks,** so the listing can show which sessions are in flight.
- **The mode-switch note** (-p ↔ REPL) and prompt-drift marks.

## What to learn from the big three

| mechanism | from | js change |
|---|---|---|
| Parallel read-only calls behind one RwLock; drain results in order | Codex `core/src/tools/parallel.rs` | done in js-1g1.11: `_dispatch_tool_calls`, `Tool.read_only` and `read_only_when` |
| Honour `Retry-After`, bigger budget, fallback model after repeated 529s | Claude Code `services/api/withRetry.ts` | `runtime.py:1813`, `_backoff` |
| Keep head and tail; spill the raw stream, not the clipped text | Codex `head_tail_buffer.rs`, pi `output-accumulator.ts` | `capped_process._StreamCapture` |
| Max-output recovery: escalate once, then resume nudges | Claude Code `query.ts:1195` | `runtime.py:1948` |
| Persist thinking signatures; replay Codex encrypted reasoning | pi `anthropic-messages.ts`, Codex `models.rs` | done in js-1g1.12: `reasoning_parts` on the assistant record, replayed to the same provider and model |
| Compaction breaker at 3 failures; text serialisation; iterative summary | Claude Code `autoCompact.ts:70`, pi `compaction/utils.ts` | `compaction.py:567`, `runtime.py:1599` |
| Cache-aware clearing; cache-break detection | Claude Code `microCompact.ts`, `promptCacheBreakDetection.ts` | `compaction.microcompact` |
| Async subagents with a completion message | Claude Code `AgentTool` `run_in_background` | `task` gets a job handle like `shell` |
| BM25 discovery; "load it first" hint on calls to deferred tools | Codex `tool_search.rs`, Claude Code `ToolSearchTool.ts` | `discovery.ranked_entries` |
| Fuzzy edit that keeps untouched bytes | pi `edit-diff.ts:132,207` | `fs._apply_edit` |
| Unchanged re-read returns a stub | Claude Code `FileReadTool.ts:528` | `_reconcile_read_delivery` |
| Record ids and parents; branching becomes a pointer move | pi `session-manager.ts:57` | `memory.Record` (before the picker) |
| Explicit `-m` beats the stamp; fallback message if the stamped model has no login; `<model_switch>` note | Codex `config_persistence.rs`, `model_switch_instructions.rs` | resume path |
| Head/tail metadata reads for listing | Claude Code `sessionStorage.ts:4744` | `session_catalog._session_details` |
| Interrupted-turn note on resume | Claude Code `conversationRecovery.ts` | resume path |
| Walk `AGENTS.md` up to a root marker, byte cap | Codex `agents_md.rs` | `config.py:628` |
| Path-scoped rules and skills | Claude Code `claudemd.ts:250` | `skills.py` frontmatter |
| Drop-in markdown commands with `$1` / `$@` | pi `prompt-templates.ts` | beside `alias` |
| Paste collapse to `[paste #N +X lines]` | pi `editor.ts:1259` | `screen.py` |
| Hooks that return context or block | Claude Code `utils/hooks.ts:418` | `events.py` |
| Kernel-to-tools bridge (`tools.read(...)` in a cell) | Codex code mode, pi codemode | `kernel.py` |

## Entirely lacking

- **Cost and cumulative token accounting.** There are per-call bench rows only.
- **Structured headless output.** There's no stream-json from `-p`, no JSON-RPC and no SDK, so another agent can't drive js as an event stream.
- **Branch, fork and rewind of history.** (designed)
- **An in-REPL picker, titles and search.** (designed)
- **Plugins, or an in-process extension API** for registering tools or rewriting calls.
- **Programmatic tool calling** (code mode). js already has the kernel, so this is the cheapest of the four to add.
- **Push notifications.** Shell and kernel jobs are pull-only, and there's no stall watchdog.
- **Worktree isolation for `task` workers.** They share one tree.
- **LSP diagnostics and a notebook-cell edit tool.**
- **Remappable keys, clipboard image paste, and cross-session prompt history** with Ctrl-R search.
- **Provider fallback, a stream idle watchdog,** and detection of silent overflow (a provider that truncates without an error).
- **Automatic memory.** Possibly already covered by the wiki pipeline, which is not wired into sessions.

## Architecture review (js alone)

Hot spots over 441 commits in two months:
- `cli.py` 76 commits, `runtime.py` 52, `fs.py` 46, `settings.py` 43.
- `cli.py` and `settings.py` changed together in 30 commits.

1. **One per-turn settings projection** (Strong, top pick). Each limit setting is copied by hand through six modules, and `fetch_timeout_s` appears in 9 files. One module that derives the effective limits gives a new setting one place to land.
2. **Split the internals of `run_turn_async`** (Strong). It is 883 lines, with 19 parameters and 13 closures. Keep the one interface and move the closures into separate modules.
3. **The REPL live-state dict** (Worth exploring). It has 165 use sites, and `provider_base_url` is written from 8 of them.
4. **Delete the compat shims in `cli.py`** (Strong, cheapest). They exist only so tests can patch old signatures, and one can mask a real `TypeError`.
5. **Move the run modes out of `cli.py`** (Worth exploring). That is about 500 lines, and `_run_prompt` takes 22 parameters.
6. **One session-file reservation module** (Worth exploring). There are two copies, and they disagree on the name format.
7. **Split code search out of `fs.py`** (Speculative). It would help navigation only.

## Priorities

1. **Shell head+tail with a raw spill.** It's cheap, and today js silently loses the one line that matters.
2. **Parallel read-only tool calls** (done in js-1g1.11).
3. **`Retry-After` plus a real retry budget.**
4. **Reasoning:** turn on thinking for direct Anthropic, and replay Codex's encrypted reasoning and Anthropic's signatures (done in js-1g1.12).
5. **Record ids plus the model stamp** (in progress in js-1g1.2), then the picker.
6. **stream-json output for `-p`.**
7. **Architecture #1 (settings projection) and #4 (delete the shims).**
