Ask to be woken after `seconds` even if no message lands. Then stop calling
tools: you sleep, and you come back with either the messages that arrived or
a `tick` from `clock` saying the time passed.

Use it when you are waiting on something that is not a message: a build you
started in the background, a reply that may never come, a check you want to
repeat later. Do not poll in the shell and do not loop on short timers; one
alarm, then sleep. `seconds: 0` clears a pending alarm.
