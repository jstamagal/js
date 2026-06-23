# aj — the ircII-style agent loop (vision / design notes)

Captured from John's design riff. The bet: agent harnesses are all
taking shots in the dark reinventing "the loop." IRC solved
extensibility 30 years ago with a tiny event set + `ON` hooks + a
scripting language (`.irc`). Point that at agents and you get
something ~10x cleaner. `ircii_dsl.py` already proves the language
works (vars, loops, aliases, recursion, event handlers); this doc is
the agent-loop application of it.

## Why ircII (not Lua/Python)
Not about IRC the protocol. It's that the `.irc` scripting *feel* is
simple, low-ceremony, and comfortable to John — and simpler than Lua
or Python for this. This is stuff that needs to get banged out and
iterated fast; the lightweight `ON`/alias idiom fits that better than
standing up a full language runtime. The abstraction is the point, not
the IRC heritage.

## The core bet
Everything the agent loop does is a typed EVENT. `ON <event>`
handlers — written in the `.irc` DSL — intercept each one. Every
cross-cutting concern normally hardcoded in core (tool gating,
validation, safety, compaction, roster, retries, status) becomes the
SAME thing: a userland `.irc` script.

Consequence: **a new agent = a new `.irc`, not a new build.** The
loop becomes data. Want a verify step, a guard, a critique pass? Load
a script. Don't fork the harness. That's exactly why IRC scripting
outlived everything — the loop was programmable.

## The loop is the program
The "agent loop" everyone hardcodes (think → act → observe → repeat)
becomes scriptable at every step. For a complex task, **each arm of
the loop gets its own script.** The agent emits a deliberate schema
for each part of the task; an `ON` hook verifies that part actually
happened before the loop moves on. The schema is the contract; the
hook is the enforcer — effectively a type system bolted onto the loop.

## Event points (the hooks)
- ON INPUT        user submits (rewrite before the agent sees)
- ON PROMPT       before request -> model (the assembled context)
- ON STREAM       a streamed token chunk
- ON TOOL_CALL    agent wants a tool, PRE-exec (allow/deny/rewrite args)
- ON TOOL_RESULT  tool returned, POST-exec (rewrite what model sees)
- ON RESPONSE / ON NOTICE   agent text / a structured marker
- ON TURN_START / ON TURN_END
- ON SUBAGENT     spawn / result (delegation)
- ON ERROR
- ON CANCEL       ESC / Ctrl-C
- ON IDLE

`^` before an event suppresses its default action (e.g. display),
ircII-style. The handler still runs.

## Handler return contract (6 modes)
1. pass     — observe only (log / metrics)
2. modify   — change the payload, continue
3. suppress — kill the default action (`^`); e.g. deny a tool / drop a line
4. inject   — feed text/result back into the agent's context
5. spawn    — kick off new behavior (tool / subagent / script)
6. loop     — flow control: reject + REPEAT the step, or abort

John named inject + spawn first; **suppress** and **loop** are the two
that make the hard cases work.

## Pattern: roster injection ("fire up a…")
When the user says "fire up a <thing>", inject the live agent roster so
the agent knows who it can delegate to. As a script:

    alias roster { fe $get_agents() name desc { _addline "[$name] $desc" } }
    ON ^usertext "*fire up a*" { my_handler($0-) }

- `get_agents` is a HOST builtin — the bridge to the live registry
  (pure DSL can't see harness state).
- `$0-` hands the whole line to the handler.
- don't fully `^`-suppress the user's text — inject the roster AND
  forward `$0-`, or the agent never sees the actual request.

## Pattern: completion-gate (no lying about "done")
The agent must emit a sentinel on completion; a hook validates it; a
lie repeats the loop. False "I'm done" is the #1 agent failure — this
makes the cure a script, not core.

    ON -notice "^NOTICE: Promise Kept$" {
        if (!$judge_done($context)) {     # cheap check OR an LLM judge
            inject "Promise NOT kept. Continue."
            retry                         # loop-control return
        }
    }

- the judge is a host builtin (`$judge_done`) OR a spawned verifier
  sub-agent — scripts loading scripts.
- CAP the retries, or a stubborn liar loops forever.
- on N strikes, escalate with `exec` (the ircII shell-out):
      exec mail me@host "got lied to twice, broke the loop"
      exec mpv rickroll.mp3        # or whatever wakes you up
  Two failures = put a human in the loop.
- this is "prove it's not lying to you," enforced at runtime instead
  of hoped for — the same ethos as testing against real behavior.

## Pattern: pretooluse guards (context-aware, scoped)
`ON TOOL_CALL` PRE-exec is the safety gate — but it's a CONDITIONAL,
not a static blocklist. It reads live state (`$cwd`, the args) and
decides per call:

    ON TOOL_CALL shell "*rm -rf*" {
        if ($cwd !~ "/tmp/scratch") halt
    }

So `rm -rf` is fine *inside* the scratch dir, fatal anywhere else.
Some loop arms legitimately need an ugly-looking command — the arm
BEFORE it verifies context and opens the gate for that one step, then
it shuts. A scoped, one-step capability grant: least privilege, not a
standing allow.

This is where `js -C <dir>` pays off: gate destructive ops on `$cwd`
being inside the scaffold the test harness set up.

## One mechanism, three jobs
Orchestrate (per-arm scripts) · validate (schema + completion-gate) ·
guard (pretooluse conditionals). All the same `ON` machinery. That's
the whole thesis in one line.

## Host builtins (the bridge)
The DSL needs host-provided functions to reach live harness state —
the seam between the `.irc` language and the harness:
- $get_agents()     the agent roster (registry map)
- $get_tools()      available tools + descriptions
- $cwd / $model     current context
- $judge_done(...)  independent verification (builtin or sub-agent)
- exec <cmd>        shell-out for side effects / escalation

## Open questions
- the minimal-but-complete event set (don't over/under-fire)
- ordering/priority when many handlers hit one event
  (ircII solved it: serial numbers + ^silent)
- sync (blocks the turn — needed for gating) vs async (logging)
- where handlers load: global / per-agent / per-session; scripts
  loading scripts
- retry caps + escalation policy to avoid infinite loops
- trust gate on `exec` (same concern as --dangerously-evaluate-inline-code)

## Relationship to ircii_dsl.py
The DSL already gives the engine: variables, loops, aliases, recursion,
AND event handlers (currently for IRC JOIN/KICK/MSG). The event system
is just: wire those same `ON` handlers to the AGENT loop's events
instead of IRC protocol events. DSL = engine. This doc = the event bus.
