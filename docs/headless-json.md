# Headless JSON Events

`js -p "..." --json` (and pipe mode, `... | js --json`) writes the run to
stdout as JSON events, one object per line. Stdout carries nothing else: the
resume hint, banners, warnings and the `--debug` trace go to stderr. The code
is `js/headless.py`; the runtime hands it every event through the
`event_sink` of `runtime.run_turn_async`.

The shape follows Codex `exec --json` (`codex-rs/exec/src/exec_events.rs`:
`thread.started`, `turn.started`, `item.*`, `turn.completed`, `error`) and
Claude Code `--output-format stream-json` (`cli/print.ts`: a closing `result`
with the session id and cost), with js's own names.

```bash
js -p "summarize README.md" --json | jq -c 'select(.type == "tool_call")'
js -p "next step" --json --session reviews/parser-fix
```

## Events

Every event has `type`. The first is `session` and the last is `result`. A
subagent's events are not in the stream; its work shows as the parent's
`task` tool call and result. Its calls are charged to the parent session too,
so they show in the `session` totals of later `usage` events and in
`result.usage`, but not in `turn_end.usage`, which counts the parent's own
calls.

| `type` | When | Fields |
| --- | --- | --- |
| `session` | once, before the turn | `version` (1), `id` (what `--session` takes; null with `--no-save`), `file` (the `.jsonl`, or null), `resumed` (the session had messages), `agent`, `model`, `provider`, `cwd`, `usage` (the session totals so far) |
| `turn_start` | the turn begins | `model`, `provider` |
| `text` | a chunk of answer text streams in | `delta` |
| `message` | a model call finishes with text | `text` (that call's whole text), `finish_reason`, `incomplete_reason` when cut off |
| `tool_call` | before a batch's tools run, one per call | `id`, `name`, `arguments` (the parsed object, or the raw string when it is not JSON) |
| `tool_result` | a call's result is recorded | `id`, `name`, `ok` (false for an error result), `bytes`, `lines`, `summary` (the first non-blank line, cut to 200 characters) |
| `usage` | after each model call charged to the session, before that call's `message` | `model`, `provider`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `cost` (dollars, null when the model has no price), `session` (the session totals after this call) |
| `error` | the turn or the run fails | `message`, `retryable` |
| `turn_end` | the turn ends | `reason` (`stop`, `incomplete`, `error`, `cancelled`, `tool_error_limit`, `max_iterations`, `retry_budget_exhausted`), `usage` (the totals of this turn's own calls), `finish_reason` and `incomplete_reason` when set |
| `sleep` | `--swarm` only: the turn ended and the agent waits on its inbox | `agent` |
| `wake` | `--swarm` only: messages landed, or a `wake_me` alarm went off; a turn follows unless every one was a `stop` | `agent`, `count`, `seqs`, `kinds` (`tick` for the alarm) |
| `result` | last line | `ok`, `exit_code`, `text` (the final answer; empty on failure), `session` (the id), `usage` (the session totals) |
| `agent_start` | `python -m js.swarm run` only: an agent of the spec starts | `agent`, `model`, `provider`, `session` (the `.jsonl`), `cwd` |
| `agent_end` | `python -m js.swarm run` only: an agent is done | `agent`, `reason` (`stopped`, `retired`, `cancelled`, `error`) |
| `run_end` | `python -m js.swarm run` only: last line | `agents`, `exit_code` |

With `--swarm ROOT/NAME` (see [swarm.md](swarm.md)) a run has many turns: each
is a `turn_start` … `turn_end`, separated by `sleep` and `wake`.

With `python -m js.swarm run SPEC.json` every agent of the spec writes to the
same stream and every event carries `agent`: the name from the spec, so a
reader demuxes by it. There is no `session` or `result` event; `agent_start`
and `agent_end` take their place per agent, and `run_end` closes the stream.

`text` deltas are not retracted. When a call fails after some text streamed
and js retries it, the deltas of the failed attempt stay in the stream; the
`message` events carry each finished call's text.

A run that fails before the turn starts, such as a bad `--reasoning` value, a
`-C` directory the jail refuses or `--debug` with `--debug-file`, writes
`error` and `result` only. Its `error.message` is the line js printed to
stderr. A command line argparse cannot parse (an unknown option) exits 2
before js knows it is a `--json` run, and writes nothing to stdout.

`usage` and totals objects carry `calls`, `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `cost` and
`unpriced_calls`; session totals add `by_model`, the same figures per model.
`input_tokens` counts every prompt token, cache reads and writes included.
`cost` sums the priced calls only. See
[Configuration And Sessions](configuration-and-sessions.md#usage-and-cost).

## Example

```json
{"type": "session", "version": 1, "id": "2026-09-29T0801-3fa2", "file": "/home/me/.js/sessions/-home-me-repo/2026-09-29T0801-3fa2.jsonl", "resumed": false, "agent": "defaultagent", "model": "gpt-5", "provider": "openai", "cwd": "/home/me/repo", "usage": {"calls": 0, "input_tokens": 0, "...": "..."}}
{"type": "turn_start", "model": "gpt-5", "provider": "openai"}
{"type": "text", "delta": "Let me read it."}
{"type": "usage", "model": "gpt-5", "provider": "openai", "input_tokens": 8120, "output_tokens": 41, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0, "cost": 0.0105, "session": {"calls": 1, "...": "..."}}
{"type": "message", "text": "Let me read it.", "finish_reason": "tool_calls"}
{"type": "tool_call", "id": "call_1", "name": "read", "arguments": {"file_path": "README.md"}}
{"type": "tool_result", "id": "call_1", "name": "read", "ok": true, "bytes": 4054, "lines": 92, "summary": "1:a1|# js"}
{"type": "text", "delta": "README.md describes ..."}
{"type": "usage", "...": "..."}
{"type": "message", "text": "README.md describes ...", "finish_reason": "stop"}
{"type": "turn_end", "reason": "stop", "usage": {"calls": 2, "...": "..."}}
{"type": "result", "ok": true, "exit_code": 0, "text": "README.md describes ...", "session": "2026-09-29T0801-3fa2", "usage": {"calls": 2, "...": "..."}}
```
