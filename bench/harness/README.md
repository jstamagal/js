# Harness bench

What each thing js does regularly costs, with the model taken out of it. The
model is `bench/swarm/fake_llm.py` in its own process, answering flat out, and
JS_HOME is a directory of the run's own.

```
just harnessbench                               # every scenario
just harnessbench tools turn                    # some of them
just harnessbench --compare ~/.js/worktrees/js-main   # that checkout's js beside this one's
just harnessbench turn --profile turn.txt       # py-spy stacks of one in-process scenario
just harnessbench --json
```

| scenario | what it measures |
|---|---|
| startup | a cold process to done: `python -c pass`, `import js.cli`, `js --list-agents`, and a `js -p` that takes one short reply. Wall and CPU milliseconds, the median of 5. |
| config | reading the layered settings (`from_env`) and settling a run (`settle`), milliseconds. |
| tools | one call of each local tool: the handler alone (`_handler_us`), and through the dispatcher a turn uses (`_dispatch_us`), which adds argument repair, the trace, the result cap, and the console and transcript rendering. Microseconds. |
| session | saving a session after each of 300 tool rounds, the last save, and replaying the 1.4 MB file. Milliseconds. |
| budget | what a model call computes before it sends, on a 600-message history: tool specs, token estimate, usage anchor, budget status. Milliseconds. |
| turn | one turn of 40 tool rounds and a 2000-token reply, in-process, drawing to a discarded stdout as `-p` does. CPU seconds. |
| idle | CPU milliseconds a second of a REPL sitting at its prompt on a pty. |
| repl | the same 40-round turn typed into a REPL on a pty and drawn on the screen. CPU seconds. |

`scenarios.py` holds the in-process scenarios; each runs in a process of its
own so one scenario's caches are not another's. `--compare DIR` runs this
checkout's scenarios against the js in `DIR`, so a scenario that needs
something only this checkout has reports an error for that side.
