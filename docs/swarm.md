# Swarm bus

`python -m js.swarm run SPEC.json` runs the agents of a swarm on asyncio loops
in a few runner processes (one per 16 agents, up to one per CPU), each agent
on a bus rooted at `ROOT`; the agents talk to each other through the bus while
they work. The bus is `js/swarm.py`, the runner `js/swarm_run.py` (see
[The runner](#the-runner)).
A program outside js reads and posts on the bus through `js.swarm.Room`.

```bash
ROOT=/tmp/troop-42
python -m js.swarm run spec.json &               # kivu and twig, both in one runner process
python -m js.swarm send $ROOT steer '*' "new evidence: the leak is in the writer"
python -m js.swarm members $ROOT                 # kivu asleep holds parser / twig working
python -m js.swarm cells $ROOT                   # the work board
python -m js.swarm post $ROOT steer "check the writer" "src/writer.py; done when the test passes"
python -m js.swarm quiet $ROOT && echo "nothing left to do"
python -m js.swarm send $ROOT steer kivu --kind stop
python -m js.swarm send $ROOT steer kivu --kind cancel             # end kivu's turn now
python -m js.swarm send $ROOT steer kivu "pick up where you left off" --kind restart
```

## What an agent on the bus gets

- **The bus tools**, added to the agent's surface by the runner; `agent.yaml`
  does not name them.
  - `send(to, text, kind)` posts to one agent or, with `*`, to everyone else.
    `kind` is a free word the troop agrees on; it is what `subscribe` matches.
  - `who` lists the names on the bus, asleep or working, and what each holds.
  - `claim(key, ttl)` and `release(key)`: one holder per key for `ttl` seconds
    (ten minutes by default). The holder renews by claiming again; a claim
    whose ttl passed is free, so a dead agent blocks nobody. A claim is not a
    message and wakes nobody.
  - `subscribe(kind, off)`: every message of that kind reaches the subscriber,
    whoever it was sent to. A disposition rather than an address: the skeptic
    wakes on `claim`, the reviewer on `done`.
  - `wake_me(seconds)`: be woken after a while even if nothing lands. The wake
    is a `tick` from `clock`. One alarm, then sleep; not a polling loop.
  - `retire(handoff)`: post the handoff to everyone, leave the bus, end after
    this turn.
  - `recruit(name, opener)`: start a sibling under a new name with this
    agent's own agent, model and settings and a session named after the
    parent's; the opener is its first prompt. The new agent runs on the same
    loop. A bus takes at most `SWARM_SPAWN_MAX` recruits.
  - `post_task(title, body)`, `take_task(id)`, `finish_task(id, result)`,
    `tasks()`: the **work board**. A posted task tells everyone with a `task`
    message and sits open on the board; taking it is a claim on `task:<id>`
    with the usual ten-minute hold, renewed by taking again and freed when it
    expires; finishing it records the result, frees the hold and tells
    everyone with a `done` message. Work sits on the board, agents take it,
    the board remembers.
- **Delivery at every tool boundary.** While a turn runs, the inbox is drained
  after each batch of tool results and the messages go in as one user message
  (a `--- messages ---` block) before the next model call. This is the same
  seam the REPL uses for lines typed during a turn (`steer` in
  `runtime.run_turn_async`).
- **Turn end is sleep, not exit.** When the model stops calling tools, the
  agent sleeps on its inbox. Nothing is sent to the model while it waits.
  When messages land it wakes into a new turn with them as the user message;
  a burst that lands within a third of a second of the first message is one
  wake. The session grows across turns like a REPL session: it is persisted
  before every sleep, and the between-turn compaction trigger runs then too,
  so a long-lived agent compacts the way a REPL session does.
- **Stop.** A message of kind `stop` ends the agent after the turn it lands in
  (or at once, if it arrives while asleep).
- **Cancel and restart.** These two kinds are for the runner, not the agent:
  the model never sees them. `cancel` ends the agent's current turn at once,
  saved as it stands; the agent then goes on as after any turn, so one that
  has seen a `stop` ends there. It is how to end a turn that never reaches a
  tool boundary and so never reads its `stop`. `restart` sent to an agent
  that has ended starts it again from its spec, on the same session, with the
  message's body as its first message, or its opener when the body is empty.
  The runner checks for them every poll while it runs. One sent to an agent
  not in that state (a `cancel` while it sleeps, a `restart` while it runs)
  is dropped, with a `dropped` event.

## Files

```
ROOT/log.jsonl            every message, one JSON object per line: seq, ts, from, to, kind, body
ROOT/claims.json          key -> holder and expiry
ROOT/cells.json           the work board: id, title, body, by, status, holder, result
ROOT/<name>/inbox/        one file per message not yet delivered to <name>
ROOT/<name>/subs          the kinds <name> subscribed to, one per line
ROOT/<name>/asleep        present while <name> waits on its inbox
ROOT/<name>/spawned       who recruited <name> and the runner's pid
ROOT/hold                 seconds an asleep agent lets messages not addressed to it pile up
ROOT/<name>/ping          the stamp of the last rollcall
ROOT/<name>/pong          the answer: ping (that stamp), ts, pid, state (asleep or working), step
ROOT/<name>/stderr        what the agent wrote to stderr under js.swarm_run, one line each
ROOT/.seq  ROOT/.lock     the sequence counter and the lock sends and claims take
```

The log is the blackboard: a runner or a UI tails it for the whole
conversation. An inbox file is written to a temp name and renamed, so a
reader never sees a partial message. A message sent to a name that has not
joined yet waits in that name's inbox, so a runner can post the opener before
it starts the agent.

The wait is a harness-side poll every quarter second. It costs no model call
and no tool call. `python -m js.swarm quiet ROOT` exits 0 when every member
is asleep with an empty inbox: the swarm has nothing to do until someone
posts. A runner uses it to call a run over without asking a model.

`js.swarm` imports with the standard library alone: troop loads it into its own
interpreter, without js's packages, to read and write the bus (`Room`). Its
imports of the rest of js are made where they are needed
(`tests/test_swarm_stdlib_only.py`).

## Hold

`ROOT/hold` holds a number of seconds; `Room.set_hold(seconds)` writes it and
it can change while the swarm runs. An asleep agent wakes at once for a
message addressed to it by name, and for a `stop`. A broadcast, or a message
that reaches it through a subscription, stays in its inbox until the oldest
one has waited the hold; then everything waiting is one wake. An agent in the
middle of a turn is not affected: its inbox is read at every tool boundary.
With no `hold` file the hold is zero.

## Rollcall

`Room.ping()` stamps a `ping` file in every member's directory. Each agent
runs a thread that looks at its own `ping` once a second and writes
`pong` with the stamp it answers, its pid, whether it is asleep or working,
and `step`: when the agent last joined, crossed a tool boundary, went to sleep
or woke. The model is not called, nothing lands in an inbox, nothing is added
to the log. `Room.rollcall(wait)` pings and collects: an answer per member,
`None` for one that stayed silent.

Silent means the agent's process is gone, since the thread answers while the
agent waits on a model or a tool. An answer whose `state` is working and whose
`step` is old is an agent that is alive and stuck inside one call. `who` and
`python -m js.swarm members` show a silent member as `no answer at rollcall
since <time>`. A retired agent has left the bus and is not called.

## Shape of a swarm

The bus does not decide who does what. There is no assign. Every agent can
address every other agent, anyone can broadcast, and the log is a wire tap,
not a controller. What makes a group of agents a swarm rather than a queue is
in their prompts: common visibility of the log, a few local rules (claim
before you touch, say when you finish, ask when stuck), and dispositions that
decide what each one reacts to. `subscribe` is how a disposition is wired;
`claim` is how two agents avoid the same file; `recruit` and `retire` are how
the troop changes shape without a runner's say-so.

## The runner

```json
{"root": "/tmp/troop-42", "slots": 0,
 "agents": [{"name": "kivu", "agent": "troop", "model": "cpa/deepseek-v4.1-flash", "effort": "low",
             "opener": "You are kivu ...", "session": "troop-42-kivu", "cwd": "/tmp/troop-42/kivu",
             "seat_file": "/tmp/troop-42/kivu.seat.md", "opts": {"SHELL_ENV_KEEP": "[\"PATH\"]"}}]}
```

Each agent is a settled run (`run_assembly.settle`, as `-p` starts from): its own config from the layered settings plus `opts`
(the same form as `--extra key=value`), its agent's prompt directory with the
contents of `seat_file` appended to the system prompt, `model` and `effort`
resolved like `-m` and `-r`, a session under its own `cwd`'s session folder
named by `session`, its own tool context with the bus tools on the surface,
the inbox drained at every tool boundary, the turn persisted and the
compaction trigger run before every sleep. The sleep is an `await` on the
inbox, so a hundred idle agents cost a few processes and no model calls.

The agents are dealt out over runner processes: `SWARM_PROCESSES`, or the
spec's `"processes"`, says how many; 0 (the default) is one per 16 agents, up
to one per CPU, and 1 runs every agent in one process. One process runs every
agent on one core, so a large swarm queues behind itself there: each runner is
a `python -m js.swarm run` of its share, and the one started relays their
events, sends `SIGINT` and `SIGTERM` on to each, and ends with one `run_end`.
The bus is files, so agents in different runners talk as agents in one do.

`slots` caps how many agents may be mid-turn at once; the rest wait for a
slot before starting their next turn. Unset, it is the registered setting
`SWARM_ACTIVE_MAX`; 0 is no cap. Over several runners the cap is dealt out
like the agents, and there are never more runners than slots. `recruit` adds an agent to the recruiter's loop with the
recruiter's agent, model and settings and a session named
`<parent session>-<name>`.

Events go to stdout as JSON lines, every one carrying `agent`. The runner adds
`agent_start`, `agent_end`, `turn_cancelled`, `restart` (with the message's
`seq` and `sender`), `dropped` (with `seq`, `message_kind` and `sender`) and
`run_end`; see
[headless-json.md](headless-json.md). An agent whose turn raises ends with
`agent_end reason=error` and the others go on. The process ends when every
agent has stopped or retired, so a `restart` has to land while another agent
is still running. `SIGTERM` and `SIGINT` cancel every agent,
persist every session and exit 130.

What an agent writes to stderr (a warning, an `on` handler's output, a
traceback) goes to the runner's stderr as `[name] line` and is appended to
`ROOT/<name>/stderr`, so one agent's errors read apart from another's. A
line a tool writes from its worker thread is not attributed and passes
through untagged.
