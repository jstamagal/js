List the agents on the swarm bus, one per line: the name, `(you)` for
yourself, `asleep` or `working`, and what the agent holds through `claim`.

These are the names `send` accepts. An agent appears once it has joined the
bus and disappears when it retires; one that has not started yet is not
listed, but a message sent to its name still waits for it. An asleep agent
wakes the moment you send to it.
