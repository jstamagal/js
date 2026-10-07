Start a new agent on this bus. It runs as a separate process with your agent,
model and settings, under the name you give, and its first prompt is your
`opener`. From then on it is a peer like any other: it reads its own inbox,
it can `send` to you and to `*`, it can `claim`, `spawn` and `retire`.

Write the opener for someone who has seen nothing: who they are, the goal,
who else is on the bus and what they do, the first thing to do, and that
they should `send` to you when they have something. A spawned agent costs a
model seat for as long as it lives, so spawn for a real job (reproduce this,
review that, build the other half) and tell it when to retire.

A name already on the bus is refused, and a bus takes a limited number of
spawned agents.
