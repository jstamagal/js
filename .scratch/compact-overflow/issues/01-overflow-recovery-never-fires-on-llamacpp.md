# Overflow recovery never fires on llama.cpp, and /compact dies exactly at overflow

Status: needs-triage
Filed: 2026-08-14
Component: `js/compaction.py` (overflow classification, manual /compact path)

## Symptom (verbatim, 2026-08-14 session, bitchx build, llama.cpp on yoda:8080)

```
error: provider 'openai' request failed; detail: Error code: 400 - {'error':
{'code': 400, 'message': 'request (97720 tokens) exceeds the available context
size (89600 tokens), try increasing it', 'type': 'exceed_context_size_error', ...
run `js --login openai`, `set provider.api_key <value>`, or `set provider.base_url <url>`

LO> /compact
compact failed: FriendlyProviderError: ... 'request (111060 tokens) exceeds the
available context size (89600 tokens)' ...
```

Owner has reported "/compact doesn't work" repeatedly. Reports were dismissed
because compaction works on providers whose error text matches the needle list.
The owner's daily provider is llama.cpp. It has never worked there.

## Root causes

1. **Detector misses llama.cpp's phrasing.** `_CONTEXT_OVERFLOW_NEEDLES`
   (`compaction.py:125-141`) has 15 needles; llama.cpp emits
   `"exceeds the available context size"` and type `exceed_context_size_error`.
   No needle matches ("context length"/"context window"/"tokens exceed" all
   miss — the actual text is "tokens) exceeds" and "context size").
   `is_context_overflow_error` → False → `recover_overflow`/`microcompact`
   never run → raw error surfaces.

2. **Manual `/compact` sends the full transcript to the model.** At overflow,
   the summarize request itself exceeds the window → 400 → "compact failed".
   The module's own comment (line 167-170) says microcompact exists precisely
   because it "cannot fail at the exact moment the context is too big to
   send" — but the manual /compact path does not escalate through it.

3. **Believed window vs server truth.** The run header shows `ctx=262144`
   (model metadata); the server was launched with `n_ctx=89600`. The
   `MAX_OVERFLOW_ROUNDS` comment (line 158-162) already admits belief can be
   wrong. The provider error *contains the real number* — parse
   `available context size (N tokens)` and clamp the session's effective
   window so round two lands instead of hitting the wall again.

4. **Cosmetic but cruel:** the error names provider 'openai' and suggests
   `js --login openai` when the provider is llama.cpp at a local URL. Error
   taxonomy work (see `fix/agent-driver-warts` branch) should cover this.

## Proposed fix

- Add needles: `"context size"`, `"exceed_context_size"`. Consider matching
  the structured `type` field explicitly, not just substrings.
- `/compact` (manual) escalates like overflow recovery: if the summarize call
  fails with overflow (or the transcript is already over the believed window
  before sending), run `microcompact` first (no model call), then summarize.
- On any overflow error, parse `(\d+) tokens\)? exceeds the available context
  size \((\d+)` and clamp `effective_context_window` for the rest of the
  session; log the clamp.

## Acceptance

On a llama.cpp server with n_ctx smaller than model metadata: blow the
context, run `/compact`, it succeeds without a manual restart. The overflow
path also auto-recovers mid-turn (three rounds max as designed).
