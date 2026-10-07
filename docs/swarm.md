# Swarm bus

`js -p --json --swarm ROOT/NAME "opener"` runs this process as agent `NAME` on
a bus rooted at `ROOT`. Many such processes, one per agent, talk to each other
through the bus while they work. The code is `js/swarm.py`.

```bash
ROOT=/tmp/troop-42
js -a troop -p --json --swarm $ROOT/kivu  "You are kivu. ..." &
js -a troop -p --json --swarm $ROOT/twig  "You are twig. ..." &
python -m js.swarm send $ROOT steer '*' "new evidence: the leak is in the writer"
python -m js.swarm send $ROOT steer kivu --kind stop
```

## What an agent on the bus gets

- **Two tools.** `send(to, text, kind)` posts to one agent or, with `*`, to
  everyone else. `who` lists the names on the bus. The tools are added to the
  agent's surface by the flag; `agent.yaml` does not name them.
- **Delivery at every tool boundary.** While a turn runs, the inbox is drained
  after each batch of tool results and the messages go in as one user message
  (a `--- messages ---` block) before the next model call. This is the same
  seam the REPL uses for lines typed during a turn (`steer` in
  `runtime.run_turn_async`).
- **Turn end is sleep, not exit.** When the model stops calling tools, the
  process sleeps on its inbox. Nothing is sent to the model while it waits.
  When messages land it wakes into a new turn with them as the user message.
  The session grows across turns like a REPL session and is persisted after
  every turn.
- **Stop.** A message of kind `stop` ends the agent after the turn it lands in
  (or at once, if it arrives while asleep). `SIGTERM` ends it like `^C`.

The headless event stream (`--json`) gains `sleep` and `wake` between turns;
see [headless-json.md](headless-json.md).

## Files

```
ROOT/log.jsonl            every message, one JSON object per line: seq, ts, from, to, kind, body
ROOT/<name>/inbox/        one file per message not yet delivered to <name>
ROOT/.seq  ROOT/.lock     the sequence counter and the lock sends take
```

The log is the blackboard: a runner or a UI tails it for the whole
conversation. An inbox file is written to a temp name and renamed, so a
reader never sees a partial message. A message sent to a name that has not
joined yet waits in that name's inbox, so a runner can post the opener before
it starts the process.

The wait is a harness-side poll every quarter second. It costs no model call
and no tool call.

## Shape of a swarm

The bus does not decide who does what. There is no assign. Every agent can
address every other agent, anyone can broadcast, and the log is a wire tap,
not a controller. What makes a group of agents a swarm rather than a queue is
in their prompts: common visibility of the log, a few local rules (claim
before you touch, say when you finish, ask when stuck), and dispositions that
decide what each one reacts to.

## Not yet

- Subscriptions by message kind or by board path; today every message to a
  name or `*` wakes it.
- Compaction between turns of a long-lived agent (the one-shot path compacts
  once, at the end).
- Running many agents in one process on the supervisor loop.
