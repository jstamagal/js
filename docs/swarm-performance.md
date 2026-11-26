# Swarm performance

What js costs per agent when many run at once, measured with `bench/swarm`
(a fake model in its own process; see `bench/swarm/README.md`). Numbers are
from a 4-core cloud VM on 2026-11-26; a bigger machine moves the absolute
figures, not the shape.

Each agent: 3 tool rounds (read, fs_search, shell), one broadcast `send`, a
200-token reply, and one more reply when the others' broadcasts wake it. The
model streams 80 tokens a second per agent, one token per chunk.

"Step gap" is the time from a model stream ending to that agent's next
request: js reading the reply, running the tool, saving, building the next
request. It is the wait a swarm feels.

| agents | build | wall | js CPU | first request (median) | step gap median | step gap p95 | peak RSS, largest process |
|---|---|---|---|---|---|---|---|
| 50 | before | 29.2s | 27.2s | 7.2s | 2.86s | 3.2s | 404 MB |
| 50 | after, 1 process | 16.1s | 13.7s | 3.9s | 0.66s | 1.4s | 143 MB |
| 50 | after, default (4 processes) | 13.3s | 25.4s | 3.0s | 0.26s | 1.4s | 112 MB |
| 100 | before | 58.7s | 60.4s | 12.0s | 5.84s | 14.7s | 646 MB |
| 100 | after, 1 process | 26.3s | 24.9s | 7.1s | 1.76s | 3.0s | 180 MB |
| 100 | after, default (4 processes) | 17.0s | 44.6s | 3.8s | 0.52s | 1.9s | 126 MB |
| 200 | before | 122.5s | 129.9s | 21.3s | 13.0s | 38.5s | 894 MB |
| 200 | after, 1 process | 74.3s | 78.1s | 13.1s | 4.09s | 29.0s | 269 MB |
| 200 | after, default (4 processes) | 26.8s | 85.3s | 5.0s | 1.13s | 1.7s | 147 MB |

"Before" is main at 69d4668. On this VM every "before" and "1 process" row
has one core pinned for the whole run: the wall time and the CPU time are the
same number. The default rows pay each runner's startup (imports, about a CPU
second each), which is why their CPU is above the one-process rows.

A sleeping swarm of 200 costs 0.05 to 0.08 CPU seconds a second (the inbox
polls and rollcall threads); that was not worth changing.

## What was slow

- **One core for the whole swarm.** `js.swarm run` ran every agent on one
  event loop in one process. Python runs one thread at a time, so 200 agents
  shared one core however many the machine has, and every agent's step waited
  behind everyone else's. The runner now deals the agents out over processes:
  `SWARM_PROCESSES`, default one per 16 agents up to one per CPU.
- **A new HTTP client for every model call.** Each call built a provider SDK
  client, which builds two TLS contexts and reads the CA bundle: 9 ms of
  blocking work per call, and a new TCP+TLS connection each time. One client
  per event loop and SDK is now shared, with each call's hooks scoped to its
  own requests by a context variable.
- **Every save replayed the whole session file.** `persist_messages` runs
  after every tool batch and read and parsed the entire `.jsonl` to find what
  was new, so a turn's saves cost grew with the square of the session. A save
  now remembers the message objects it last wrote and the file's size; when
  both still match it appends the rest without reading. Saving 300 tool
  rounds into one session: 1.75s before, 0.77s after, and flat per save.
- **The `.txt` transcript and search index rewritten on every tool batch.**
  They now catch up when the turn ends.
- **The model catalog's sqlite opened several times per model call** to ask
  whether it is stale. The answer is reused for 60 seconds while the catalog
  and status files are unchanged.
- **Tool descriptions re-rendered for every model call** (a regex pass over
  each description). Cached by description and surface.
- **A flush per streamed token** on the swarm's event stream. Text and
  reasoning deltas now go out with the next event or within 50 ms.
- **Session paths resolved (`realpath`) several times per save.** Once per
  path now.
- From the test-suite work that this builds on: every colour and message
  lookup walked the project chain (`paths.find`).
