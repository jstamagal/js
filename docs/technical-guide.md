# Technical Guide

This is the architecture guide for contributors and agents working on `js`.

## Package Entry Points

`pyproject.toml` exposes:

```toml
[project.scripts]
js = "js.entry:main"
```

`js/__main__.py` delegates to the same CLI path.

## Runtime Shape

One normal prompt run follows this path:

1. `js.cli.main()` parses flags and hands a `-p` run to `js.prompt_run.run()`.
2. `js.run_assembly.from_env()` builds `Config`.
3. `js.persona.load_prompt_spec()` loads the selected agent from repo
   `defaults/agents/`, global `~/.js/agents/`, and project `.js/agents/`.
4. `ToolRegistry.select()` filters the default registry by prompt selectors.
5. Existing session messages are loaded through `js.memory.load_replay_messages()`,
   which keeps every assistant's reasoning for replay.
6. `js.conversation.Conversation.turn()` appends the new user message, which
   `-p` writes to the session file before the model is called.
7. `js.runtime.run_turn_async()` loops over model calls and tool calls.
8. The turn is saved after each batch of tool results and when it ends.

The REPL uses the same runtime but keeps the message list in process and
persists each completed turn.

## Module Responsibilities

| Module | Responsibility |
| --- | --- |
| `js/cli.py` | `main`: the run's setup, the mode picker, reading stdin into the prompt, `--compact` and `--commit` dispatch |
| `js/prompt_run.py` | the `-p` run (`run`): its steps from the prompt to the printed answer, its `on` handlers, its stats, the Continue line (`say_continue`) |
| `js/command_line.py` | the argument parser, and argv read the way it reads it |
| `js/cli_options.py` | invocation-local prompt and tool overrides the modes share |
| `js/bench.py` | `--bench AGENT`: an agent over its benchmark cases, one row per case |
| `js/commit_cli.py` | `--commit`: snapshot the worktree, then run the commit agent over it |
| `js/listings.py` | the `--list-*` printers, as text or JSON |
| `js/migrate_config.py` | `--migrate-config`: a legacy `config.toml` as a jsrc set-script |
| `js/run_assembly.py` | config, agent and model settled into a run (`settle`), the turn `-p`, the REPL, `--bench` and swarm agents start from |
| `js/routing.py` | the provider and model route a run resolves to, and its credentials |
| `js/conversation.py` | a session's history across turns (`Conversation.turn`), as `-p` and swarm agents run them |
| `js/history_record.py` | the history record of a message, and the SDK message it becomes |
| `js/swarm.py` | the swarm bus: inboxes, the room log, claims, the work board, the bus tools |
| `js/swarm_run.py` | every agent of a swarm in one process (`python -m js.swarm run SPEC.json`) |
| `js/repl.py` | the interactive REPL: startup (`start_repl`), the input loop, the turn queue and steering, the status bar, the `/session` switch |
| `js/repl_commands.py` | the REPL's slash commands: the command table (`COMMANDS`) assembled from the `repl_*_commands` rows, dispatch, `/help`, `/load`, aliases and the startup run of the jsrc files |
| `js/repl_agent_commands.py` | `/persona`, `/tools`, `/skill`, `/skills`: the system prompt, tools and skills a turn carries |
| `js/repl_hook_commands.py` | `/on` and `/exec`: a command run on an event, and the shell command whose stdout is queued for the next user message |
| `js/repl_job_commands.py` | `/jobs`, `/cancel`, `/flush`: the supervisor's jobs |
| `js/repl_window_commands.py` | `/window`: ircII's op chain over `screen.Windows` and the subagent windows |
| `js/repl_model_commands.py` | `/model`, `/pick-model`, `/provider`, `/baseurl`, `/apikey`, `/login`, `/logout`, `/models`, `/refresh-model-catalog`; the model stamp a turn would write (`live_stamp`) |
| `js/repl_session_commands.py` | `/reset`, `/wipe`, `/turns`, `/cost`, `/copy`, `/session`, `/name`, `/compact`, `/quit`: the session file and the conversation in it |
| `js/repl_settings_commands.py` | `/set`, `/fset`, `/show`, `/save`, `/compact-auto`: the effective `/set` view, what `/save` compares against (`mark_saved`), and live state following a changed setting (`apply_settings_result`) |
| `js/repl_workspace_commands.py` | `/cd`, `/add`, `/drop`: where the session works, and a resumed session put back there (`restore_workspace`) |
| `js/repl_state.py` | the REPL's live state (`ReplState`), its assembly, the command-table row (`Command`), and carrying live settings into the route and tool registry |
| `js/config.py` | environment parsing, session reservation, model/provider caps, vision heuristic |
| `js/settings.py` | the setting registry (`REGISTRY`, `SettingSpec`), the defaults/jsrc layer (`default_value`, `knob`), the live settings store (`live_value`) |
| `js/setting_values.py` | what a setting's value can be: text checked and read into a value (`coerce_value`), and a value spelled as a jsrc line holds it (`config_value_text`, `jsrc_text`) |
| `js/jsrc_line.py` | how one jsrc line reads: its words and verb (`first_word`, `split_command`) and a string value spelled on it (`line_literal`, `from_line_literal`) |
| `js/setting_layers.py` | the config layers over defaults/jsrc (jsrc files, env vars, `--extra`) and their collection (`collect_settings`) |
| `js/jsrc_save.py` | `/save`: the changed settings and /fset entries written into the user jsrc, in place (`save_settings_to_jsrc`) |
| `js/turn_settings.py` | the settings a turn reads: one row each, projected from the settings store onto `Config` and `ToolContext` |
| `js/model_client.py` | the model I/O boundary: one streamed call over the Vercel AI Python SDK (`stream_model_async`), its client and wire protocol |
| `js/provider_errors.py` | a provider's failures, named: which are retried (`retryable`), `FirstByteTimeoutError`, `StreamIdleError` |
| `js/stream_protocols.py` | the wire protocols a stream is read through: Anthropic thinking and redacted thinking, `reasoning_content`, Responses |
| `js/request_params.py` | a request's parameters: sampling per wire, output and reasoning budgets, the prompt-cache key |
| `js/stream_transport.py` | the request's HTTP layer: response ownership, the network channel (`NetCall`), the first-byte wait |
| `js/network_display.py` | the network channel's notices and failures, and its installed sink (`NetSink`, `install_sink`) |
| `js/owning_loop.py` | a coroutine run to its end on a loop of its own (`run_owning_loop`), for sync callers |
| `js/debug_trace.py` | the request-trace autolog (`DebugTraceSink`), its path, the request trace (`emit_request_trace`) and the stdout tee `-d` writes through |
| `js/runtime.py` | the turn loop (`run_turn_async`), recording replies and tool results |
| `js/turn_dispatch.py` | a turn's tool batch run: reads beside reads, writes alone, fan-out on the loop, cancellation (`dispatch_batch`) |
| `js/tool_calls.py` | the tool calls a model made, made fit to run and to record: aliases, argument repair (`repair_jsonish`), the assistant message |
| `js/tool_errors.py` | the same error from the same tool counted across a turn (`ToolErrorTracker`) |
| `js/result_caps.py` | a tool result held to its byte caps (`cap_result`, `cap_batch_results`) |
| `js/tool_result_messages.py` | a tool result as the model gets it: provider text, history record, SDK messages with media |
| `js/context_window.py` | a model's context window (`resolve_context_window`), its overrides, and token estimates |
| `js/console.py` | js's console sink (`ConsoleSink`): draws output events on stdout or the screen, folds tool runs (`ToolTrace`); `trace_call`, `trace_result`, `emit_event` |
| `js/turn_stream.py` | a turn's events (`TurnEvents`) and its streamed reply as output events (`StreamSink`) |
| `js/output.py` | output events (`OutputEvent`), the `Sink` a turn emits them to (`Telemetry.sink`), the list of the loop's events and their fields |
| `js/display.py` | answer text on its way to the terminal: the speakers' marks, Markdown rendering (`render_markdown`), the answer stream (`Display`), the colours (`Theme`) |
| `js/tool_display.py` | a tool exchange as the screen draws it (`render_exchange`, `render_fold`), and as the transcript records it (`for_transcript`) |
| `js/resume_view.py` | a resumed session's last exchanges, in the order a turn drew them (`resume_parts`) |
| `js/text_layout.py` | text in terminal columns: `cells`, `cut_points`, wrapping, the terminal's width |
| `js/control_sequences.py` | model and tool text kept from acting as terminal control sequences (`StreamCleaner`, `clean`) |
| `js/transcript.py` | the transcript log (`TranscriptLogSink`), its stdout tee, and its record of output events (`TranscriptSink`) |
| `js/turn_surface.py` | the session file's record of the lazy tool surface, restored per agent and directory |
| `js/turn_budget.py` | the turn's SDK conversation (`TurnConvo`) and the context budget it is held to (`TurnBudget`) |
| `js/turn_call.py` | one model call with its retries, overflow recovery, escalated resend and signed-reasoning resend (`ModelCaller`) |
| `js/memory.py` | locked JSONL persistence and loader control marks |
| `js/message_times.py` | when each message happened, noted as it happens and taken when its record is written |
| `js/compaction.py` | the "history is too big" decision and the summary that replaces it (`compact_now`) |
| `js/compaction_settings.py` | the compaction settings a run reads, each with its jsrc default when unset or out of range |
| `js/microcompact.py` | old tool results cleared in place, for overflow recovery (`recover_overflow`) or a turn's budget (`clear_for_budget`) |
| `js/compaction_summary.py` | the summary request: the conversation serialized for the summarizer, the prompt, the call |
| `js/compaction_flight.py` | the flight record of every compaction attempt (`flight_log_dir`, `printing`) |
| `js/usage.py` | per-session token and cost totals, the `usage` records, the meter a turn charges its calls to |
| `js/headless.py` | `js -p --json-stream`: typed runtime events as JSON Lines (docs/headless-json.md); `json_run` wraps a run so stdout ends with its result |
| `js/messages.py` | every string the operator reads, as named entries; the banner slot; each entry's default format |
| `js/persona.py` | prompt-directory concatenation and `agent.yaml` |
| `js/events.py` | event names, `on` handler table, handler refusals, the approval a program passes to `run_turn_async` |
| `js/hookexec.py` | `exec`: runs a command for the command layer; event JSON on stdin, exit 2 refuses |
| `js/prompt_commands.py` | `~/.js/commands/NAME.md` as `/NAME`, argument placeholders |
| `js/pastes.py` | large bracketed pastes kept behind `[paste #N ...]` markers |
| `js/toolkit/core.py` | `Tool`, `ToolContext`, argument coercion, handler invocation |
| `js/toolkit/registry.py` | default registry assembly, per-agent surfaces, lazy catalog |
| `js/toolkit/policy.py` | `noun:modifier` chains, `tools.yaml` tags and argument bans, `/tools` table |
| `js/toolkit/fs_tools.py` | the fs suite's tool table, and the path-skill offer on read, write and patch results |
| `js/toolkit/fs.py` | read, write, remove and undo tools; the file byte and text helpers and the stale-read guard patch and the searches share |
| `js/toolkit/patch.py` | patch tool: exact and fuzzy string replacement behind the read guard |
| `js/toolkit/file_search.py` | fs_search (ripgrep), its child processes, and stopping them on cancel |
| `js/toolkit/path_hints.py` | the hint for a path that does not exist: nearest directory, close names, same-tail paths |
| `js/toolkit/shell.py` | shell tool: commands, their background jobs and the output it reports |
| `js/toolkit/fetch.py` | fetch tool: http, https and file:// requests, read inline or saved with `save=` |
| `js/toolkit/plan.py` | plan tool |
| `js/toolkit/skill.py` | skill tool |
| `js/toolkit/subagents.py` | task and generated agent tools |
| `js/toolkit/results.py` | tool results: the adapters output sinks share, and the spill and cut that bound what a model gets |
| `js/toolkit/presentation.py` | how the screen shows a tool (`Presentation`), and its result parsed for drawing |
| `js/toolkit/reads.py` | the read ledger: what of each file the model has seen, so an edit of unread or changed lines is refused |
| `js/toolkit/undo.py` | undo snapshots, in memory and, for a saved session, in a bounded store on disk |
| `js/toolkit/wiki/` | deterministic tools for installed wiki agents |

## Prompt Loading

An agent directory's `.md` files are named by the last word of the stem:
`NN-prompt.md`, `NN-env.md`, `NN-benchmark.md`, `NN-user.md`, `NN-agent.md`.
Only those load; any other `.md` (README, CHANGELOG, notes) is not read. The
system prompt is the `*-prompt.md` and `*-env.md` files, sorted by filename and
concatenated with blank lines. `--system-prompt TEXT` stands in for the
`*-prompt.md` files and keeps everything else about the agent; `--no-env`,
`--you-are`, `--chat` and `--system-prompt-file` leave out the `*-env.md`
files. The manifest is `agent.yaml` in the same
directory:

```yaml
model: cpa/claude-fable-5-1
reasoning: high
tools:
  - read:eager
  - fs_search:eager
  - "wiki_*:lazy"
  - task:eager
```

`tools` entries are `noun:modifier` (`eager`, `lazy`, `ban`) or `tag:NAME`;
resolution and `tools.yaml` are described in [tool-system.md](tool-system.md).
No entries means no tools exposed to the model. A `00-tools.yaml` or a
frontmatter `00*.md` fails the load with a line naming `agent.yaml`; `just
migrate-agents` converts them.

### Message files

`N-user.md` and `N-agent.md` initialize a new conversation, ordered by the
numeric `N` (any width; `-` or `_` separators). Matching numbers form an
exchange: the user file is recorded as a user message, then the agent file as
an assistant message, without calling the model. A user file without a matching
agent file runs a normal model turn; it shows only with `-d`. An agent file
without a user file records an assistant message on its own. Bare `user.md`
and `agent.md` are exchange 0. Two files defining the same numeric role are an
error.

A fresh session of an agent with message files never opens the REPL. With no
`-p` the exchanges are the whole run: the last one's answer is printed as a
`-p` answer is, and js exits. With `-p` the prompt is sent after the last
exchange, its answer printed, and js exits. A session that already has
messages skips initialization, so resuming one opens the REPL.

Message contents retain their whitespace and are expanded when reached, using
the same [prompt expansion](inline-directives.md) as system text. In saved
sessions, every message and generated reply is saved before the next file expands, so an inline command
can inspect the preceding exchange through `%%CURRENT_SESSION_FULLPATH%%`.
They remain ordinary session history on resume; initialization is only applied
to conversations without prior messages. Interrupted setup retains its recorded
prefix as history. This applies to the REPL, `-p`, pipe
runs, and task agents. Other filenames, such as `01-troop.md`, `02-ape.md` and
`03-env.md`, including symlinks, continue to supply system text.

### Benchmark mode

A `benchmark.md`, `N-benchmark.md`, or other filename ending in `-benchmark.md`
or `_benchmark.md` selects benchmark mode automatically for the resolved agent.
`js --agent codebench` runs its benchmarks rather than entering the REPL; the
explicit `js --bench codebench` form also works. Even an empty benchmark file
selects this mode (and reports no runnable benchmarks rather than entering the
REPL). Explicit maintenance modes such as `--commit` retain their own behavior.

The user/agent setup exchanges run once, before the first benchmark. Each
benchmark then starts a fresh, unsaved conversation from a copy of that setup
and sends the benchmark body as its user message, so every benchmark sees the
same context, including the reply of any setup user file that ran a live model
turn. Benchmark text expands after that setup. Conversations do not carry
across benchmarks. A benchmark's stats count only its own turn; when setup
called the model, its calls are reported once as a `setup` row (and a `setup`
key in `--stats-json`). Benchmark frontmatter may set `max_tokens`; `--max-out`
overrides it. Timing and token statistics are printed, with `--stats-json` and
`--stats-csv` available for reports.

`js --loop 10`, `--loop 45M`, and `--loop 3H` repeat fresh invocations by
completed turns or elapsed time; bare `--loop` runs until Ctrl-C. Time limits
are checked between invocations, so an active turn finishes. Each child receives
the original options and working directory, excluding `--loop`. Piped input is
replayed for every child. Without a prompt or benchmark, the loop supplies a
one-shot instruction to carry out the agent's assigned task, so it does not open
an idle REPL. A counted loop stops on a failed child. Continuous and timed loops
retry failed children with backoff, stopping after five consecutive failures.

## Default Registry

`build_default_registry()` assembles tools in this order:

```text
fs_tools.py       file tools
shell.py          shell
fetch.py          fetch
web_search.py     web search, docs search and browse
jev.py            jev
terminal.py       terminal sessions
browser.py        browser probe
plan.py           plan
skill.py          skill
subagents.py      task
wiki/             wiki tools
python_kernel.py  kernel
toolbox.py        toolbox
lsp.py            lsp
notebook.py       notebook editing
js/swarm.py       swarm tools
generated prompt-directory agent tools
```

Generated agent tools come from directories with markdown files under repo
`defaults/agents/`, global `~/.js/agents/`, and project `.js/agents/`. Files layer per relative name: project > home > defaults when roots define the same agent id. A
prompt directory whose name collides with an existing tool is skipped.

## Tool Context

`ToolContext` is mutable process-local state:

- `cwd`
- read limits and file size caps
- tool result and shell output caps
- fetch timeout
- vision enabled flag
- the read ledger (`reads`) behind read-before-write checks
- undo snapshots
- search cache

`run_turn_async()` hydrates the active context from `Config` each turn: the
`on_context` rows of `js/turn_settings.py` (`turn_settings.install`), agent id,
selected registry, and vision mode. In the REPL the Config itself is
re-projected from the live settings store before each turn
(`turn_settings.project`), so `/set` of any row takes effect on the next turn.

Child task contexts copy the parent's cwd, model and `on_context` rows
(`turn_settings.inherit`) but start with fresh read sets, snapshots, and
search cache.

## Runtime Loop

`run_turn_async()` mutates the caller's `messages` list. It builds a provider `convo`
by prepending the system prompt to the current messages.

`run_turn_async` in `js/runtime.py` is the loop itself: set the turn up, then
per iteration call the model, record the reply, run the tool batch and record
its results. The parts it drives live in their own modules, which never import
`runtime`: `turn_stream` (events and the streamed reply), `turn_surface` (the
tool-surface journal), `turn_budget` (the SDK conversation and the context
budget) and `turn_call` (one model call and its recovery). Each has a unit-test
file of the same name. `turn_budget` takes the context-window lookup as a
callable, so `context_window.resolve_context_window` stays the one place it
is resolved, and `turn_call` reaches the model through
`model_client.stream_model_async` on the module.

The loop writes nothing to the screen itself. Every visible thing it produces
(tool calls and results, the run line, call stats, the answer and the
reasoning, notices) is an `OutputEvent` with structured fields, sent to
`Telemetry.sink`; `js/output.py` lists the events. Without a sink of its own a
telemetry uses js's: the transcript record (`transcript.TranscriptSink`), which
writes each tool call whole and the answer to the transcript log, then the
console (`js/console.py`), which draws them as js always has. The run line,
call stats and notices are recorded from events while transcript stdio capture
is active; command output continues through its tee on stdout and stderr.
A program that sets `Telemetry(None, sink=...)` gets the events and js
prints nothing. Beside the text the model gets, read, patch, write, remove and
shell hand the screen their result as fields with `toolkit.core.view` (path,
text before and after, line numbers, exit code, stdout and stderr); the
tool_result event carries them as `view`.

1. Builds `ai` message and tool parts via `js/model_client`.
2. Calls `model_client.stream_model_async(...)` through `turn_call.ModelCaller`.
3. Streams events; `model_client` aggregates text, reasoning content, and fragmented tool calls.
4. Parses each completed tool-call argument string with `tool_calls.repair_jsonish`.
5. Dispatches through `call_tool` (`js/turn_dispatch.py`) and appends tool-result messages.
6. Repeats until the model returns a stop or `TURN_ROUNDS_MAX` model calls are spent.
7. Stops on tool retry limit or max iterations.
The model-client stream scope owns HTTP response byte iterators for the OpenAI
and Anthropic SDKs. `stream_transport.py` closes each iterator before its pool
entry on normal completion or cancellation. This covers SSE termination before
HTTP EOF with httpx2 2.12 (upstream pydantic/httpx2#1195). Custom transports are
left as supplied. Recheck this adapter when upstream stream ownership changes.

The same wrapper carries the `NET_DISPLAY` channel. `stream_model_async` opens a
`NetCall` per request (`stream_transport.begin_call`); the response iterator
feeds the channel's handshake line, and a
`trace` request extension reports the TCP/TLS handshake as "connected" (the
first token stands in when the transport cannot say). Who is calling (main
turn, `Subagent N`, `Compacting`) is a context variable set by
`run_turn_async` and the compaction call sites. `run_turn_async` retries, so
its role holds each request failure instead of printing it: the next request
drops it, and the turn prints it (level 1) only when it gives up. The retry
itself prints at every level (`retry.wait` emits `retry_wait`, a
`retry_tick` per second and `retry_done`; `retry_gave_up` with the last
failure when the budget is spent): the console prints the failure and the
wait, and counts it down on stderr under `-p` or on the status bar through
`TurnStatus`. The give-up line names the last failure, so the held one is
dropped. After each
response `compaction.note_response` compares its cache-read tokens with the
previous response of the same conversation and model; a drop of more than 5%
and at least 2000 tokens prints one cache-break line at level 2. A compaction or
tool-result clearing resets that baseline, because the drop it causes is
expected; so do `/reset` and `/wipe`. A response without usage keeps the
baseline. The channel
prints only while the async REPL has installed a sink; elsewhere every hook is
a no-op, and the models.dev refresh lines print to stderr as before.

`run_turn_async` also starts a usage meter (`js/usage.py`, a context variable)
naming the session file and the `ToolContext.usage_chain` above it. Each
model call it makes, and each compaction summary made inside it, is charged
to those sessions; a summary made outside a turn is charged to the session it
compacts. The meter's callback adds the call to the turn's totals, which
`turn_end` carries, and hands it to the `event_sink` as a `usage` event.

Provider request retry:

- One rule decides what is retried: `provider_errors.retryable`, applied in
  `model_client._stream_async` as a failure leaves the SDK, which sets the error's
  `is_retryable` for every retrying caller. A status in `RETRY_STATUSES`
  (429, 502, 503, 504, 529) is retried; 500 and every other 4xx fail at once. An
  Anthropic `error` event inside a stream that answered 200 counts as the
  status its type stands for (`error_status`), and an OpenAI Responses stream
  error as the status its `code` stands for (`rate_limit_exceeded` 429;
  `server_error`, `internal_error`, `model_error` 500). A network failure is
  retried: refused, past `CONNECT_TIMEOUT`, or a connection dropped before
  or during the reply. Any other answer with no status is not.
- A failure the rule retries is retried up to
  `RETRY_COUNT_MAX` times (default 3). Each retry waits what the
  response's `retry-after-ms` or `Retry-After` header asks (seconds or an HTTP
  date), else a flat 8s by default (no jitter). A wait longer than
  `RETRY_WAIT_MAX` (default 120, 0 means no limit) fails the
  request at once. `_open_stream` sets the OpenAI and Anthropic SDK clients'
  `max_retries` to 0, so these retries are the only ones. The turn runs them
  itself (`turn_call.ModelCaller`), between overflow and max-output recovery; every other model
  request (compaction summaries, the `/login` test) goes through
  `retry.call` under the same budget.
- Three clocks bound a request (`retry.StreamLimits`, 0 is no limit for
  each). `CONNECT_TIMEOUT` (default 15) is the TCP and TLS handshake, laid
  on each request's httpx timeout by `stream_transport.own_responses`, which
  also clears the transport's read timeout. `_stream_async` runs the request
  under one `asyncio.timeout`: from the request being sent to the first
  response body byte it is `FIRST_BYTE_TIMEOUT` (default 0), the prefill;
  when it expires the request fails once with `FirstByteTimeoutError`, not
  retried. From the first byte on it is `STREAM_IDLE_TIMEOUT` (default 300),
  moved forward on every chunk the transport reads (SSE keep-alive comments
  included) and every SDK event; when it expires the request fails with the
  retryable `StreamIdleError`. Compaction's summary calls get the same clocks
  and the same retries. The wait for the first byte shows as `waiting for
  first byte 4m12s` on the status bar (`TurnStatus.waiting_since`) and,
  under `js -p` on a terminal, on its own stderr row
  (`stream_transport.FirstByteWait`).
- Context overflow has three separate recovery rounds, each followed by a new
  request. Recovery clears old tool results, then summarizes if needed. This
  also works after tool execution: only the rejected model request is retried.
- A reply the provider accepted although its reported input tokens exceed the
  window is a silent overflow: the provider cut the input to fit. So is a
  reply cut off by its cap with no output and an input filling 99% of the
  window. The window is the larger of the model's (`context_window.resolve_context_window`)
  and `CONTEXT_SIZE`, so a catalog that understates the window can be
  corrected there. A silent overflow spends an overflow round the same way
  and the request is sent again. When reply text is already on the screen or
  stdout, the reply is kept instead and the shed history serves the next
  request, so no reply is printed twice. The SDK's `input_tokens` already
  include cache reads.
- Unrecovered provider errors propagate to the caller.

Max-output recovery. A reply is cut off by its cap when a Responses stream marks
it incomplete with `max_output_tokens` or the SDK's finish reason is `length`
(OpenAI `length`, Anthropic `max_tokens`), and it has no tool call that can
run. Then:

1. Once per turn, the same request is sent again with
   `OUTPUT_TOKENS_RETRY_MAX` (default 64000) as the cap, held to the
   model's known output limit and to what the window leaves after the
   prompt. This happens only when that is larger than the cap the cut reply
   had, so a model already at its limit or a full window skips it. It is also skipped when reply text is already on the
   screen or stdout, since a resend would print it twice. The cut reply is
   discarded. If the provider rejects the larger cap with a non-retryable
   error, a context-length error included, the request is sent again at the
   configured cap without shedding history.
2. A reply still cut off is recorded, with any truncated tool call dropped,
   followed by a user message marked `resume_nudge` telling the model to
   resume. This repeats up to `OUTPUT_RESUMES_MAX` times per turn
   (default 3). After that the turn ends `incomplete`.

## Tool Dispatch

Tool call arguments arrive as JSON strings, often in fragments. The runtime
concatenates fragments by call id and repairs common JSON mistakes:

- outer JSON string that decodes to JSON
- trailing commas
- missing final `}` for object-shaped args

Unknown tool calls return an `ERROR` result naming available tools. A name a
proxy cloaked (`promote_shell`, `mcp__exile_poem__fs_search_placeholder`) is
repaired first: without its `mcp__<server>__` prefix, `_placeholder` suffix
and, if need be, its first `<word>_`, it dispatches when it names exactly one
published tool, and the flight log records `tool_name_repaired`.

Tool errors are tracked per tool, as a count of the same error in a row
(numbers, paths and quoted text do not make two errors different). Each
`ERROR` gets retry metadata:

```text
<retry>attempts_left=2, allowed_max_attempts=3</retry>
```

When one error repeats `TOOL_ERRORS_MAX` times, the model is told once to stop
retrying that tool and change approach. A different error or a success starts
the count over. One more repeat of the same error ends the turn on a final
assistant error instead of surfacing "no assistant response".

## Task and Read-Only Parallelism

`task` calls from the same assistant turn are dispatched concurrently. The
other calls from that turn run in model order under a readers-writer rule:
read-only calls next to each other run together, up to
`PARALLEL_TOOLS_MAX` at once (default 8; 1 runs every call in turn),
and a call that writes runs alone, after every call before it. Read, read,
patch, read runs as [read ∥ read] → patch → read. Result messages are restored
to original tool-call order before being appended. See
[Dispatch Semantics](tool-system.md#dispatch-semantics) for which calls count
as read-only and how shared state is kept consistent.

Inside the `task` tool, multiple task strings run concurrently on the parent's
event loop. `SUBAGENT_WORKERS_MAX` caps the subagents running at once across
the whole process; a parent blocked on its children lends them its slot.
Cancelling the parent cancels
its foreground children in both the REPL and one-shot mode.

See [Subagents](subagents.md).

## Images And Vision

`read` returns an image marker (`IMAGE_RESULT\t<path>\t<mime>\t<stub>`) when
the model is vision-capable; other tools return image or audio blocks. The
history record of the result (`tool_result_messages.tool_result_record`) keeps
the text and lists the media under `media.MEDIA_KEY`; the bytes are stored once
by content under the media directory (`js/media.py`), so the session file
stays small and every later request, resumed or not, sends the same bytes.

Image result shape (`history_record.history_to_ai_messages`):

- the tool-result message carries the text
- after the batch's tool messages, a user message per result carries a label
  naming the call plus a `FilePart` for each media part
- a text-only model gets a note that the image was dropped instead

## Built-In Modes

`--commit` (`js/commit_cli.py`) is a convenience wrapper around the
prompt-directory `commit` agent: it snapshots the worktree, then runs the agent.

## Commit Helper

`js.commit_helper` is a standalone CLI the `commit` agent shells out to for the
deterministic parts of committing: surveying repo state and splitting one file
across commits. These belong in code, not the model
(`js/commit_helper.py:1`). Its subcommands are `survey`, `stage` and `commit`;
each accepts `-C <dir>` / `--repo <dir>` (or `--repo=<dir>`) before the
subcommand to target a repo explicitly instead of the process cwd
(`_extract_repo`). With no repo flag the helper resolves `Path.cwd()`
(`_repo_path`).

`python -m js.commit_helper survey` prints one compact snapshot the agent reads
once instead of probing (`cmd_survey`). Its lines are `SURVEY_*` entries in
`js/messages.py`:

- `Branch:` line (`branch --show-current`, falling back to `Detached HEAD at
  <hash>` or `No commits yet`)
- `-- Status --`: raw `git status --porcelain` `XY path` rows, or
  `Clean tree. Nothing to commit.`
- the staged and the unstaged diff sections: per-tracked-file text diffs with
  every `@@` hunk numbered, each headed `### <path>: N hunks`. Only the
  unstaged heading names the `stage <file> <n[,n]|all>` form, since only
  unstaged hunk numbers address `stage`.
- `-- Untracked --`: `??` files, each with the `stage <p> all` hint, or `None.`
- `-- Recent log --`: `git log --oneline -8`, or `No history.`

The survey is deterministic: it only reads git state (status/diff/log), runs no
model, and does not mutate the repo. A file with no text hunks says so and
tells the agent to stage the whole file.

`python -m js.commit_helper stage <file> <hunks|all>` stages part of one file
(`cmd_stage`). `<hunks>` is a comma-separated list of the 1-based hunk numbers
the survey printed (e.g. `1,3`), or `all`:

- For a tracked text file, the named hunks are extracted from `git diff -- <file>`
  and replayed with `git apply --cached --recount`; output is
  `Staged <file>: hunks 1,3 of N`.
- `all` on any file stages the whole file via `git add -- <file>`.
- An untracked (`??`) file only accepts `all`; a hunk spec on it fails.
- Out-of-range hunk numbers, non-numeric specs and files with no pending
  changes each fail with exit code `2`; a `git apply` failure exits `1` and
  prints the `stage <file> all` fallback.

For a bare or unknown subcommand `main()` prints `COMMIT_HELPER_HELP` on
stderr and exits `2`; `-h` / `--help` prints it on stdout and exits `0`.
`stage` or `commit` with the wrong argument count exits `2`. A non-git
directory exits `2`; other git failures exit `1`.

## Error Boundaries

The runtime generally returns tool failures to the model as strings rather than
raising. Provider and harness failures raise to the CLI.

The CLI prompt mode catches runtime exceptions and returns exit code `1`.

The REPL catches runtime exceptions and keeps the REPL alive. Completed tool
work and partial replies are persisted; an unstarted prompt is discarded using
its current position, including after compaction has shifted the history.

## Operator Messages

The operator's messages are named `Message` entries in `js/messages.py`; code
passes the values for their holes (`msgs.say(msgs.MODEL_SET, model=m)`,
`msgs.warn(...)` for stderr, `.text()` for a string raised as exception text).
A banner entry prints behind the `BANNER` slot (`***` today); no template
spells the slot. Each entry is shown through its /fset format (`js/fset.py`):
its fields become the words `$0`, `$1`, ... and the format adds colour only
where it writes `%Cnn`; `defaults/fset` draws a failure's glyph red and a warning's
orange.
`say` paints only a stream that is a terminal, so a pipe gets plain lines.

`Message.said(...)` is `.text()` that keeps its entry: a `str` whose `message`
and `fields` say what produced it. A REPL command returns its refusal that
way, and the REPL prints it through that entry's format, so a failed save
shows its red glyph and a usage slip shows none. `compact_now` returns its result the same way,
and `compaction.compacted()` tells a compaction from a skip by the entry.

The `/help` column (`CMD_*`), `js --help` (`SHORT_HELP`), every argparse help
and description (`OPT_*`) and argparse's own headings and refusal go through
entries too; `msgs.ArgumentParser` wires the last two. `SettingSpec` docs stay
in the settings registry beside their key, and the kernel panel's field
labels stay in the panel. The commit helper prints its entries with
`.text()`, because the commit agent reads its output: no colour, no slot.
Tool results, tool descriptions and prompts are the model's text and stay
where they are.

`tests/test_messages.py` fails on a literal argparse help, description,
`parser.error` or command doc in `js/`, and on a setting doc with a paren
aside. It drives `--help`, `--help-full`, a bad flag, the `/tools` table, a
failed prompt directive and the commit helper, and fails when their output
carries a paren aside or one of `error:`, `warning:`, `js:`, `note:`, `(no `,
`knob` or `!!`.

## Backward Compatibility Policy

This project intentionally does not preserve old tool aliases. The canonical
surface is the contract: the tools `build_default_registry()` assembles, each
with its model-facing text in `defaults/tool_descriptions/<name>.md`, plus the
generated prompt-directory agent tools (`commit`, `defaultagent`, ...).

`multi_patch`, `sem_search`, `followup` and the `artifact` suite were removed
outright; `multi_patch`'s batch form now lives in `patch` as its `edits`
parameter.

Tests should protect current behavior, not old names.


## Context-window resolution

Remote model context limits come from models.dev. Catalog refresh keeps release
dates alongside limits in the local SQLite cache. Exact provider/model IDs are
resolved first; routed prefixes and effort suffixes are normalized. Family
`latest` aliases select the newest matching release date while preserving
variant names. Manufacturer rows resolve equal-release reseller duplicates.
A family lookup is an estimate of an alias target, not router configuration.

Ollama, llama.cpp and vLLM can use their actual allocated context. Explicit
model overrides remain available. `CONTEXT_SIZE` applies one window to the
run banner and both automatic compaction paths;
`CONTEXT_SIZE_UNKNOWN` is the window assumed for unresolved models;
`defaults/jsrc` sets it to 1,000,000.

Both automatic compaction paths cap reply headroom with
`COMPACTION_REPLY_RESERVE_TOKENS` and reserve `COMPACTION_HEADROOM_TOKENS`.
Output truncation alone does not trigger summarization. Between-turn compaction
is awaited on the async CLI loop, so cancellation stops its provider request.
In-turn compaction
prints its reason and budget before summarizing; automatic compaction marks
retain phase, context-token count, window and effective input limit.


### Compaction flight records

Every compaction js runs writes a unique attempt under
`logs/<agent>/compactions/<session>-<attempt>.jsonl`. `COMPACTION_LOG_DIR`
changes that directory. The directory is the `log_dir` argument of
`compact_now`, `recover_overflow` and `clear_for_budget`; js's callers pass
`compaction.flight_log_dir(cfg)`, and None writes no record. Records contain caller stack/PIDs, active model and
settings (secret settings and credential fields always masked), exact system/messages before and after,
content hashes, retention decision, summary input/output and provider trace,
plus success, skip, failure or cancellation. Runtime-triggered records also
include the tool schemas, request budget and usage anchor. Files are created
with mode 0600; records are flushed and fsynced before summarization starts.

Terminal START and outcome notices include attempt ID and file path. They
reach the caller's `emit` as `compaction_notice` events; `compaction.printing()`
is the `emit` js passes, which prints them as `*** Compaction ...` lines on
stderr and hands every other event on. Session
compaction marks carry that same ID. Budget telemetry also goes to the existing
request autolog as `FLIGHT` JSON records even when optional runtime debug is off.
`/set CONTEXT_SIZE N` immediately displays the effective next-request
window, and between-turn compaction reads the same live settings.

Overflow recovery also records `operation=tool-result-clearing` attempts before
replacing old tool-result bodies. START/outcome notices go to stderr even when
answer stdout is redirected. Flight data includes the provider rejection,
retry round, retained-result count, changed message indexes and tool-call IDs,
character savings, and complete before/after context. No eligible results is
recorded as SKIPPED; any following summarization has its own attempt ID.

### Compaction commit and replay

In-turn budget recovery clears old tool results (`COMPACTION_KEEP_TOOL_RESULTS`
starts the retained-result count), summarizes the history before the current
user message, and, if the active turn itself is too large, summarizes its older
work while retaining a paired assistant/tool tail. Empty prefixes and
summary-only prefixes are not summarized. The order depends on the prompt cache.
When the last request is at least `COMPACTION_CACHE_TTL` old (default 300,
0 = always), or the provider has already refused the request, clearing runs
first. While the cache is warm, the summary of earlier turns runs first, and
clearing runs only if that summary did not bring the request under budget.
Clearing always runs before the current turn is summarized, because that summary
rewrites the whole history too.

`COMPACTION_FAILURES_MAX` (default 3) automatic summaries that fail in a
row pause automatic compaction, in-turn and between turns, and print one line
saying so. One budget check makes at most one failed summary attempt. While
paused, tool-result clearing still runs, warm cache or not. A successful
summary, such as a manual `/compact`, resets the count and resumes it.
Both trigger paths use current provider-anchored input plus generated output;
output-only usage falls back to estimation. Small windows share the same capped
reserve and buffer calculation.

The summary request is plain text, not JSON: one `[User]`, `[Assistant]`,
`[Assistant tool calls]` or `[Tool result name]` paragraph per message inside
`<conversation>`. Tool results, tool-call arguments and reasoning longer than
`COMPACTION_SUMMARY_RESULT_CHARS_MAX` (default 2000, 0 = whole) keep their head
and tail. When the prefix starts with an earlier `<compaction-summary>`, that
summary goes in `<previous-summary>` and the model is asked to update it with
the new messages, so a second compaction carries the first one forward instead
of summarizing it as conversation. A re-attached files message is reduced to its
paths.

Summary overflow partitions the source and summarizes both halves, with bounded
split depth. The previous summary travels with the older half only. A failing partition, blank response, or incomplete response leaves
the source history intact. The proposed replacement must shrink the history;
optional file reattachment is omitted if it consumes those savings.

Pending messages are journaled before a compaction mark. The mark carries any
reattached files, and clearing mutations are journaled as replacements. CLI and
child-agent persistence compare the current live history with replayed history,
append changed suffixes, and retain original records in the archive. Cancellation
uses current user position rather than a pre-compaction list offset.

`tests/compaction_harness/` contains standalone adversarial runners: scripted
provider-boundary failures and a loopback HTTP/SSE server exercising the actual
SDK adapter. See [Compaction adversarial audit](compaction-adversarial-audit.md).

### Idle cache keepalive

`js/cache_keepalive.py`. Every model call of a turn records the request it
sends on its ToolContext (`cached_request`): messages, tools, route, effort,
sampling, headers, cache key and the usage meter, everything but the
callbacks and the output cap. While the REPL waits for input, a loop beside
the input line looks every 5 seconds. At 90% of `COMPACTION_CACHE_TTL` after
the cache was last read (at least 10 seconds before it runs out) it resends
the main context's recorded request with `max_output_tokens=1`. The replay is
the same prefix the provider cached, so it is a cache read that renews the
entry; it bills that read plus one output token, charged to the sessions the
request was charged to, and prints `NET_CACHE_KEEPALIVE` at `NET_DISPLAY` 2.
A refresh sets `last_request_at`, so compaction ordering sees a warm cache.
The status bar shows the next refresh as `STATUS_KEEPALIVE`.

Only Claude routes are refreshed: the Anthropic wire with
`ANTHROPIC_PROMPT_CACHE` on, and a model whose id says claude behind any
other wire. CLIProxyAPI adds the cache breakpoints itself to a translated
Claude request (system block and last message) and, on an OAuth login, gives
them the 1h ttl, so there `COMPACTION_CACHE_TTL 3600` matches what the
provider does and costs one refresh an hour. A request with `THINKING_BUDGET`
set is not replayed: a one-token cap changes the budget, and a thinking change
invalidates the cached messages. OpenAI caches automatically, keeps entries
for a retention it decides, and its Responses API takes no output cap under
16, so no GPT route is refreshed.

Refreshing stops for the rest of the idle stretch when the session has been
idle `CACHE_KEEPALIVE_IDLE_MAX` seconds (0: no refreshes), when a refresh reads
less than half its prompt from the cache, when a refresh fails, when the cache
had already run out (the machine slept), and silently when the live model or
provider changed. A history rewrite (`compaction.history_rewritten`) forgets
the recorded request. A turn records a new one and starts a new stretch.

At the idle limit, with `CACHE_KEEPALIVE_COMPACTION` on and the last
context at least `CACHE_KEEPALIVE_COMPACTION_TOKENS_MIN` tokens, the keepalive
queues `conversation.idle_compact_async` on the turn queue, where it runs as a
turn does (^C cancels it) and summarizes the history, so the request that ends
the idle stretch writes a small prompt. The summary request is the plain-text
`<conversation>` shape, not the conversation's own prefix, so it does not read
the cache; it costs the same whether the cache is warm or not.
