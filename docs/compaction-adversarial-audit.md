# Compaction adversarial audit

Baseline: `67a6500`. The initial failing code was exercised independently of the
existing pytest helpers. Both standalone runners live in `tests/compaction_harness/`.

```sh
uv run python tests/compaction_harness/harness.py "$PWD" "$HOME/inbox/agents/js/compact-probe"
uv run python tests/compaction_harness/wire.py "$PWD" "$HOME/inbox/agents/js/compact-wire"
just test-file tests/test_compaction_adversarial.py
```

Use a fresh output directory per run. The first runner drives real compaction,
runtime, CLI persistence, and replay with deterministic provider outcomes. The
second runs an HTTP/SSE server on loopback, through the real SDK and model client.
Numbered facts, actual tool invocation counts, request captures, and replayed
histories are the oracles. These check mechanics, not a model's summary quality.

## Reproduced failures and corrections

| Path | Measured failure | Correction |
|---|---|---|
| Summary overflow | Eight source facts became two after half-dropping retries | Summarize both partitions; abort replacement if any fails |
| Invalid summary | Blank and explicitly truncated responses replaced source | Require completed nonempty text before commit |
| Summary savings | 96 characters grew into 9,123; empty source was summarized | Skip empty/summary-only prefixes and nonshrinking replacements |
| In-turn budgets | Repeated high usage rewrote only the prior summary | Clear old results, then summarize new history/active work as needed |
| Tail selection | A 100-token tail included a 60,000-character old message | Check the next message against remaining tail space; preserve tool pairs |
| Replay | Reattached files and cleared tool bodies differed after restart | Journal mutations and include rehydration in compaction marks |
| Active-turn save | Current user was summarized away and saving raised | Persist the current list against journal state, not old user identity |
| Child isolation | Parent files appeared in child compaction | Pass the active tool context for calibration and rehydration |
| Between-turn count | 6,000 input plus 3,200 output was counted as 6,000 | Read current token tracker |
| Skip outcome | A skipped summary incremented successful-compaction count | Count committed replacements only |
| Calibration | A prefix including output was calibrated against input alone | Compare the matching input-plus-output scope; reject stale prefix |
| Missing input usage | A large request became a 20-token output-only anchor | Fall back to estimation when prompt usage is absent |
| Small windows | Between-turn limit 2,000; in-turn limit zero | Cap the combined reserve/buffer consistently |
| Recovery retries | Third recovery changed history but got no subsequent request | Separate transport retries from overflow rounds |
| Mid-turn overflow | Rejection after one tool aborted immediately | Retry the rejected model call without replaying tool effects |
| Interrupted compacted turn | Old list offsets lost partial work | Find current user position or retain already-compacted work |
| Async compaction | Ctrl-C returned while executor summary later committed | Await cancellable async policy directly |

## Interpretation

A constant over-limit usage fixture can still cause repeated compactions of
**new** work: it deliberately says every subsequent request is too large. The
property is progress and source coverage, not an arbitrary maximum summary count.
The wire cache fixture reports 63,000 input and 62,900 cache hits; the adapter and
budget count input once and trigger zero summaries.

The seeded journal exercise makes 60 append/edit/compact/resume transitions and
compares replay with live state after each. Cancellation and failed-partition
probes check unchanged source, while overflow recovery checks exactly-once tool
execution. Baseline comparison counts include several checks of the same defect;
they are not a claim of that many distinct bugs.

Historical session replay established all twelve actual history transformations.
It did not recover every trigger-time usage anchor. Cache double-counting and
summary-only rewrites form a reproduced failure mechanism; the newly exposed
loss/replay/async defects are additional mechanisms, not invented attributions
for historical marks whose telemetry was absent. See #113 and #117.

## Verification

Final standalone fault runner: 25/25 scenarios pass. Loopback SDK runner: 2/2
scenarios pass. Full offline suite: 1,705 passed; `just lint` passed. Captures
include provider requests, summary flights, journal/replay pairs, and the seeded
60-step sequence. No live-provider credentials are needed to rerun these checks.
