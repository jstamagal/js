# Swarm bench

What js costs the machine per agent, with the model taken out of it.
`fake_llm.py` is a stand-alone chat-completions server (standard library only,
its own process) that streams a scripted reply at a set token rate; `run.py`
points a swarm or a pile of `js -p` processes at it and measures js from the
outside.

```
just swarmbench swarm 100                    # 100 agents on one bus
just swarmbench swarm 200 --processes 1      # all of them in one runner process
just swarmbench swarm 50 --tps 0             # the model streams as fast as it can
just swarmbench procs 50                     # 50 concurrent `js -p` processes
just swarmbench swarm 100 --js-root ~/.js/worktrees/js-main   # another checkout's js
just swarmbench swarm 50 --processes 1 --profile prof.txt      # py-spy stacks of the runner
```

Each agent runs `--steps` tool rounds (read, fs_search, shell), then a reply of
`--reply-tokens` tokens. On the bus it also broadcasts one `send`, so every
other agent wakes once and replies, then the swarm goes quiet and the bench
sends `stop` to all.

| column | what it is |
|---|---|
| wall_s | start until every agent is asleep again |
| cpu_s, cpu_per_agent_s | user+sys of the js processes (the fake model not counted) |
| startup_median_s, startup_max_s | start until each agent's first model request |
| gap_median_ms, gap_p95_ms, gap_max_ms | a stream ending until that agent's next request: js reading the reply, running the tool, saving and building the next request |
| idle_cpu_per_s | CPU seconds a second the swarm burns while every agent sleeps |
| rss_mb | the largest js process's peak resident memory |

The gap is the number a swarm feels: the model is done and the agent is
waiting on js. The tools are cheap on purpose, so it is nearly all harness.
Results for the changes this bench was built for are in
`docs/swarm-performance.md`.
