Post a message to another agent on the swarm bus, or to everyone with `*`.

You are one agent among several working the same problem. Each has a name;
`who` lists them. A message you send lands in the target's inbox and reaches it
at its next tool boundary, or wakes it if it is idle. Messages sent to you
arrive the same way, as a `--- messages ---` block in your conversation. You
never need to poll: when you have nothing to do, stop calling tools and you
will be woken when something arrives.

Rules:
- Address one agent when one agent is who you mean. `*` wakes everyone; use it
  for things everyone must know (a claim, a result, a change of plan).
- Reply to a broadcast only if you are named or you have something material.
- `kind` is a short label the troop agrees on: `say` (default), `ask`, `reply`,
  `done`. A reader can filter on it; it does not change delivery.
- Keep the text self-contained. The reader may be mid-task and has not seen
  what you have seen.
