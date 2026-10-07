Get every message of a given `kind`, whoever it was sent to. A message sent
to one agent with that kind reaches you as well, and wakes you if you are
idle. Your subscriptions stay until you turn them off with `off: true`.

Use it to react to a class of event rather than to being named: a skeptic
subscribes to `claim` and `result`, a reviewer to `done`, a reproducer to
`bug`. The kinds are whatever the troop agreed to pass to `send`.

Messages sent to you or to `*` reach you with or without subscriptions.
