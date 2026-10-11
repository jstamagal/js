List the agents on the swarm bus, one per line: the name, `(you)` for
yourself, `asleep` or `working`, and what the agent holds through `claim`.
An agent whose process did not answer the last rollcall shows `no answer at
rollcall since <time>` instead: it is gone or hung, and a message to it waits
unread. Give its work to someone else.

These are the names `send` accepts. An agent appears once it has joined the
bus and disappears when it retires; one that has not started yet is not
listed, but a message sent to its name still waits for it. An asleep agent
wakes the moment you send to it.
