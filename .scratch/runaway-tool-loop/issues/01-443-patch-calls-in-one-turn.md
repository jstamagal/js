# One degenerate message carried 443 identical empty tool calls — js executed all of them

Status: needs-triage
Filed: 2026-08-14 (rewritten same day after session-log verification)
Component: `js/runtime.py` (tool dispatch), tool-call validation

## Verified mechanism (session 20260814T162102202806Z-2f0a5f428290e5ca)

Not a retry loop across turns. The log shows:

- ONE assistant message with a `tool_calls` array of **443 entries**
- `distinct_args = 1` — all identical, arguments **`{}`** (empty)
- All 443 ids are 32-char mixed-case alphanumeric — llama.cpp's
  `random_string()` format. Neither js nor the ai SDK mints ids (verified by
  grep). The server itself emitted 443 entries.
- Attribution: llama.cpp's incremental tool-call stream parser is the prime
  suspect — empty-args entries with fresh server ids = parser opening calls
  without attaching argument bodies. The MTP/speculative build shifts chunk
  boundaries, aggravating incremental parsing. The model was mid-coherent
  work; a model loop would repeat complete calls with args, not empties.
  Definitive proof needs yoda-side verbose generation logs or a repro.
- js dispatched **all 443**. Every result: `ERROR: path is required`
  (some with `<retry attempts_left=...>` decoration adding more tokens)
- 443 identical error results ballooned the transcript to ~97k tokens →
  context overflow → cascade into `.scratch/compact-overflow/issues/01`
  (detector missed llama.cpp phrasing, /compact could not fit either;
  session unrecoverable)

## Problems, in dispatch order

1. **No pre-dispatch schema validation.** `patch` requires a path; args `{}`
   is invalid on its face. An invalid call should fail cheap and once, not
   execute.
2. **No dedupe of identical calls within one message.** 443 × identical
   (name, args) should execute once and reuse the result — or reject the
   batch as degenerate.
3. **No sanity cap on the array.** A 443-entry tool_calls array from one
   message is not plausible work from any model. Cap (configurable, ~50) and
   reject the remainder with one message to the model.
4. **No collapse of identical results.** 443 copies of the same error is
   pure context poison. Consecutive identical tool results should collapse
   to first occurrence + "repeated N times".

Small local models make degenerate generations routine, not rare. The
harness is the only sane party in the loop; it must be the adult.

## Proposed fix (in order of payoff)

1. Dedupe identical (name, args) within a message: execute once, fan out the
   single result. One-screen change, kills 442/443 of the damage.
2. Reject calls that fail the tool's required-arg schema before dispatch.
3. Per-message tool_calls cap with a clear model-facing error.
4. Collapse consecutive identical results when appending to the transcript.

## Acceptance

Replay a synthetic assistant message with 200 identical empty patch calls:
one execution, one error in transcript, turn continues; context grows by
one result, not two hundred.
