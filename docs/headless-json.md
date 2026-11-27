# Headless JSON

`--json` prints one final object on stdout. `--json-stream` prints JSON Lines
while a run executes. They are mutually exclusive. Banners, warnings, traces
and Continue commands go to stderr, keeping stdout machine readable.

```bash
js -p "summarize README.md" --json
js -p "summarize README.md" --json-stream | jq -c 'select(.type == "tool_call")'
js --bench slim --json
js --list-agents --json
js --verify --json
```

A prompt result includes `answer`, `session`, `provider`, `model`, `effort` and
`usage`, plus the existing statistics payload (`agent`, `turns`). A failed run
reports `ok: false` and an `exit_code`. `--no-save` leaves `session` null.
When a turn reaches its output or round limit, or the provider refuses the
request, `stop_reason` is `max_tokens`, `max_turn_requests` or `refusal`.

A benchmark keeps its original `agent`, `model`, `provider` and `benchmarks`
object. Each row adds `tools`: names in call order with repeats retained;
an unknown call is prefixed `?`. The final JSON object printed by a plain bench,
`--bench --json`, and a stream's `end` payload is the same shape.

## Stream events

Each line has `type`. A run starts with `start` and finishes with `end`, whose
other fields are the object printed by `--json`.

| Type | Content |
| --- | --- |
| `start` | Session and run identity: id, file, resumed, agent, model, provider, cwd, effort and usage where available. |
| `reasoning` | Reasoning text in `delta`. |
| `text` | Answer text in `delta`. |
| `tool_call` | Call id, name and arguments; parsed JSON when valid. |
| `tool_result` | Call id, name, result, success flag and summary. |
| `retry` | Attempt, total attempts, delay and error message. |
| `error` | Error message and whether it is retryable. |
| `end` | The final result object. |

A run that an agent's user files repeat with `%%LOOP%%` is one run here:
`--json` prints the one object of its last repeat, and `--json-stream` carries
each repeat's records after the first run's, every repeat opening with its own
`start`, and ends with one `end`, the last repeat's result. `ok` and
`exit_code` are those of the whole loop.

Deltas from a failed attempt remain in the stream if the request is retried.
Subagent work appears through the parent's task call and result. Session usage
includes calls charged by subagents; individual call statistics count the
parent's calls. Usage totals include calls, input/output/cache/reasoning tokens,
cost and unpriced calls. See [Configuration And Sessions](configuration-and-sessions.md#usage-and-cost).

## Listings and refusals

`--list-models [PROVIDER]` and `--list-agents` have text and JSON forms.
`--list-sessions`, `--list-providers` and `--list-logins` require `--json`.
`--verify --json` returns verification findings as a JSON object.

`--json-stream` requires a run, such as a prompt, agent or benchmark; it cannot
stream a listing. A command with no requested JSON form exits 2 and names the
command. Removed flags (`--list`, `--models-json`, `--providers-json`,
`--logins-json`) are unknown options, with no abbreviation aliases.

The implementation is `js/headless.py` plus CLI output routing. The separate
`python -m js.swarm run` interface is documented in [swarm.md](swarm.md).
