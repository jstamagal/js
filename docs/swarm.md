# Swarm bus

`js -p --json --swarm ROOT/NAME "opener"` runs this process as agent `NAME` on
a bus rooted at `ROOT`. Many such processes, one per agent, talk to each other
through the bus while they work. The code is `js/swarm.py`.

```bash
ROOT=/tmp/troop-42
js -a troop -p --json --swarm $ROOT/kivu  "You are kivu. ..." &
js -a troop -p --json --swarm $ROOT/twig  "You are twig. ..." &
python -m js.swarm send $ROOT steer '*' "new evidence: the leak is in the writer"
python -m js.swarm members $ROOT                 # kivu asleep holds parser / twig working
python -m js.swarm cells $ROOT                   # the work board
python -m js.swarm post $ROOT steer "check the writer" "src/writer.py; done when the test passes"
python -m js.swarm quiet $ROOT && echo "nothing left to do"
python -m js.swarm send $ROOT steer kivu --kind stop
python -m js.swarm run spec.json                 # the same swarm, every agent in one process
```

## What an agent on the bus gets

- **The bus tools**, added to the agent's surface by the flag; `agent.yaml`
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
    parent's; the opener is its first prompt. One process per agent: a new
    process whose events go to `ROOT/<name>/events.jsonl`. In an in-process
    swarm: a new agent on the same loop. A bus takes at most eight recruits.
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
  process sleeps on its inbox. Nothing is sent to the model while it waits.
  When messages land it wakes into a new turn with them as the user message;
  a burst that lands within a third of a second of the first message is one
  wake. The session grows across turns like a REPL session: it is persisted
  before every sleep, and the between-turn compaction trigger runs then too,
  so a long-lived agent compacts the way a REPL session does.
- **Stop.** A message of kind `stop` ends the agent after the turn it lands in
  (or at once, if it arrives while asleep). `SIGTERM` ends it like `^C`.

The headless event stream (`--json`) gains `sleep` and `wake` between turns;
see [headless-json.md](headless-json.md).

## Files

```
ROOT/log.jsonl            every message, one JSON object per line: seq, ts, from, to, kind, body
ROOT/claims.json          key -> holder and expiry
ROOT/cells.json           the work board: id, title, body, by, status, holder, result
ROOT/<name>/inbox/        one file per message not yet delivered to <name>
ROOT/<name>/subs          the kinds <name> subscribed to, one per line
ROOT/<name>/asleep        present while <name> waits on its inbox
ROOT/<name>/spawned       who recruited <name>, its pid and command
ROOT/.seq  ROOT/.lock     the sequence counter and the lock sends and claims take
```

The log is the blackboard: a runner or a UI tails it for the whole
conversation. An inbox file is written to a temp name and renamed, so a
reader never sees a partial message. A message sent to a name that has not
joined yet waits in that name's inbox, so a runner can post the opener before
it starts the process.

The wait is a harness-side poll every quarter second. It costs no model call
and no tool call. `python -m js.swarm quiet ROOT` exits 0 when every member
is asleep with an empty inbox: the swarm has nothing to do until someone
posts. A runner uses it to call a run over without asking a model.

## Shape of a swarm

The bus does not decide who does what. There is no assign. Every agent can
address every other agent, anyone can broadcast, and the log is a wire tap,
not a controller. What makes a group of agents a swarm rather than a queue is
in their prompts: common visibility of the log, a few local rules (claim
before you touch, say when you finish, ask when stuck), and dispositions that
decide what each one reacts to. `subscribe` is how a disposition is wired;
`claim` is how two agents avoid the same file; `recruit` and `retire` are how
the troop changes shape without a runner's say-so.

## Every agent in one process

`python -m js.swarm run SPEC.json` runs every agent of a swarm on one asyncio
loop in one process. The code is `js/swarm_run.py`.

```json
{"root": "/tmp/troop-42", "slots": 0,
 "agents": [{"name": "kivu", "agent": "troop", "model": "cpa/deepseek-v4.1-flash", "effort": "low",
             "opener": "You are kivu ...", "session": "troop-42-kivu", "cwd": "/tmp/troop-42/kivu",
             "seat_file": "/tmp/troop-42/kivu.seat.md", "opts": {"limits.shell_env_allow": "[\"PATH\"]"}}]}
```

Each agent gets what a `--swarm` process gets, built the way the task tool
builds a child turn: its own config from the layered settings plus `opts`
(the same form as `--extra key=value`), its agent's prompt directory with the
contents of `seat_file` appended to the system prompt, `model` and `effort`
resolved like `-m` and `-r`, a session under its own `cwd`'s session folder
named by `session`, its own tool context with the bus tools on the surface,
the inbox drained at every tool boundary, the turn persisted and the
compaction trigger run before every sleep. The sleep is an `await` on the
inbox, so a hundred idle agents cost one process and no model calls.

`slots` caps how many agents may be mid-turn at once; the rest wait for a
slot before starting their next turn. Unset, it is the registered setting
`swarm.slots`; 0 is no cap. `recruit` inside this runner adds an agent to the
loop with the recruiter's agent, model and settings and a session named
`<parent session>-<name>`; its `spawned` marker says `"inproc": true`.

Events go to stdout as JSON lines, every one carrying `agent`. The runner adds
`agent_start`, `agent_end` and `run_end`; see
[headless-json.md](headless-json.md). An agent whose turn raises ends with
`agent_end reason=error` and the others go on. The process ends when every
agent has stopped or retired. `SIGTERM` and `SIGINT` cancel every agent,
persist every session and exit 130.
