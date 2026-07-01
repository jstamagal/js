# js × irciipy — non-blocking, event-first, windowed

Design notes. Branch `nonblocking-windows`. Nothing here changes default
behavior yet; every step lands behind a flag.

## North star

- js NEVER blocks the REPL. Typing always works.
- A turn and its subagents are background tasks. Cancel any, anytime (ESC).
- Every piece of js output is a **structured event**. irciipy formats it.
- Windows = ircII model: many windows, ONE full-screen at a time, switch /
  swap / kill / hide / resize, bind a window to a key. Hidden windows keep
  running.

## Three load-bearing pieces

### 1. Agent identity: `model!provider@baseurl`

Every running agent/subagent gets an IRC-style identity (`nick!user@host`):

    opus-4-8!anthropic@api.anthropic.com

- nick = model, user = provider, host = baseurl.
- This string is the `source` on every event that agent emits.
- Windows bind to a source glob. irciipy already globs (`*` `%`):
  `/window bind opus*!anthropic@*`.
- Subagent N is just another identity → its own window, addressable.

### 2. The event schema (all output is an event)

Nothing in the turn path writes to stdout. It emits an `OutputEvent`:

    name    one of the canonical set (stream, tool_call, tool_result,
            response, reasoning, notice, error, subagent, set_row, ...)
    source  the model!provider@baseurl that produced it
    args[]  ordered positional values  -> irciipy $0 $1 $2 ...
    fields{} named values (rich hooks, structured logs)
    text?   optional default rendering (fallback when no hook fires)

- js ships a DEFAULT irciipy script that formats each event. **No hardcoded
  English in the hot path.**
- Example — `/set` list does NOT print rows. It emits `set_row` events with
  `args=[key, value]`. Default hook:

      on set_row * {echo $G $0 = $1}

  You rewrite the format string, not js.

### 3. Non-blocking runtime

Full async, not worker threads (fork B resolved — see below).

- The REPL owns the ONE event loop. `prompt_async` keeps input live; turns and
  subagents are `asyncio.Task`s on that loop, tracked by `js/supervisor.py`.
- `model_client.stream_model_async` / `runtime.run_turn_async` are the real
  primitives; the sync `stream_model`/`run_turn` are thin `asyncio.run`
  wrappers kept for one-shot `-p`, bench, and tests.
- Tools are sync (subprocess/file IO) → each turn's tool dispatch runs in
  `run_in_executor`, so the loop stays free while tools execute.
- Subagents schedule onto the SAME loop from the tool-dispatch thread via
  `run_coroutine_threadsafe`, as cancelable `subagent` jobs — not a private
  loop (that would leave them detached and un-cancelable).
- Output paints above the live input line via `patch_stdout(raw=True)` today;
  it moves to events → irciipy → target window later.
- Cancel = `task.cancel()`. Ctrl-C cancels the active turn; the turn's
  `CancelledError` handler persists partial work and heals orphaned tool_calls.
  A running tool batch finishes detached (v1 contract) — a per-task flag
  checked between sequential tools is the later upgrade.

## Windows (the view)

- A window = a scrollback buffer + a binding (which sources/events it shows).
- Screen shows ONE window full-size (or a split when asked). Others live
  hidden.
- `/window new|switch|swap|kill|hide|resize|bind`, plus window→key bind.
- Low-vision default: one big window. Splits are opt-in, never forced.

## irciipy → library

Today it is a near-complete REPL. Turn it into an embeddable lib:

- Public API: `feed_event(OutputEvent) -> rendered lines (+ target window)`.
- Expose: symbol table, hook registry, expand/format, command dispatch.
- js owns the loop + windows; irciipy owns formatting + scripting + hooks.
- The seam already exists: `js/events.py EventHooks.set_dispatcher(...)`.
  irciipy becomes that dispatcher.

## Build order (each behind `--nonblocking`, default OFF)

0. **Event schema + OutputSink** — DONE. `js/output.py`: `OutputEvent`, `Sink`,
   `StdoutSink`, `agent_identity`. Data contract only; not yet on the hot path.
1. **Async runtime + supervisor** — DONE. `stream_model_async` /
   `run_turn_async` primitives; `js/supervisor.py` job registry; `--nonblocking`
   REPL (`_repl_main`/`_turn_consumer`/`_do_turn`) with `prompt_async` +
   `patch_stdout`; subagent fan-out on the shared loop as cancelable jobs.
   (Supersedes the old worker-thread plan.)
2. **Wire output through events.** Route `run_turn_async`'s stdout writes
   through `OutputEvent` → `Sink`; default sink stays byte-for-byte stdout.
   ← NEXT.
3. irciipy as dispatcher: events → ON hooks → rendered text.
4. `/window` commands + key bindings (`/jobs`, `/cancel` are the seed).
5. `model!provider@baseurl` identity on every event; window↔source binding.
6. Per-task cancel flag checked between tools (kill running tools, not just
   detach); fire-and-forget subagents into windows.

## Open forks

- **A. Topology (open).** irciipy in-process (one binary) vs out-of-process
  daemon + view (your `ircii-go/bitchtea` shape). Assumed default: in-process
  first, daemon later — the event stream is the wire protocol either way.
- **B. Concurrency (RESOLVED → full async).** Worker threads were the fallback;
  we went full async instead. Reversible if it disappoints.
