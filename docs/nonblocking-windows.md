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

- The REPL (a prompt_toolkit `Application`) owns the ONE event loop. Always
  reading keys.
- A turn = a background task. Worker thread first; async task later.
- `model_client.stream_model` calls `asyncio.run()` per call → it MUST run off
  the UI thread. Worker thread is step 1.
- Subagents already run in a `ThreadPoolExecutor` — same mechanism, surfaced.
- Output crosses back to the UI via a thread-safe queue, drained on the UI
  loop, fed to irciipy, routed to the target window.
- Cancel = a per-task flag the runtime checks between chunks / tool calls. ESC
  sets it. (Partial work is already preserved.)

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

0. **Event schema + OutputSink.** Route the turn's stdout through events.
   Default sink = print (today's behavior, byte-for-byte). ← START HERE;
   needed in EVERY future, in-process or daemon.
1. Worker-thread turn: run_turn off the main thread; output via queue.
2. prompt_toolkit `Application` shell: input always live; one window.
3. irciipy as dispatcher: events → ON hooks → rendered text.
4. `/window` commands + key bindings.
5. `model!provider@baseurl` identity on every event; window↔source binding.
6. Cancel/ESC per task; fire-and-forget subagents into windows.

## Open forks (your call — do NOT block step 0)

- **A. Topology.** irciipy in-process (one binary) vs out-of-process
  daemon + view (your `ircii-go/bitchtea` shape). Assumed default: in-process
  first, daemon later — step 0's event stream is the wire protocol either way.
- **B. Concurrency.** Worker threads vs a full async rewrite of model_client.
  Assumed default: threads first (smaller, reversible), async later.

Both defaults are reversible. Step 0 commits to neither.
