Ask to be woken after `seconds` even if no message lands. Then stop calling
tools: you sleep, and you come back with either the messages that arrived or
a `tick` from `clock` saying the time passed.

Use it when you are waiting on something that is not a message: a build you
started in the background, a check you want to repeat later. A message wakes
you by itself, so an alarm adds nothing to waiting for a reply, a review or a
result. Do not poll in the shell and do not loop on short timers; one alarm,
then sleep. The alarm stays set when a message wakes you first, and a second
call while one is pending changes nothing. `seconds: 0` clears a pending alarm.
