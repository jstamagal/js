# Session writer double-logs: prefix diff re-appends already-persisted records

Status: needs-triage
Filed: 2026-08-14
Component: js/cli.py `_persist_unrecorded_messages`, js/memory.py load healing

## Owner's report, verified

Owner: "I think js is double logging, trust my instinct" — and "I've seen
weirdness with double logging in js before." Confirmed by forensics on
session 20260814T162102-2f0a5f: the file held 22 duplicated tool calls and
22 duplicated results (44 IntegrityError issues on resume under the new
loader), while the debug autolog shows every request payload the runtime
sent was clean — no id ever appears twice in memory. Disk-only duplication
= writer bug, not model/parser.

## Mechanism

`_persist_unrecorded_messages` (cli.py:216) computes how much of memory is
already on disk via `_common_message_prefix_len` — EXACT dict equality.
Both sides drift after the original append:

- in-memory messages get mutated post-append (tool retry decorations,
  canonical tool-name rewrites per runtime.py:138, reasoning fields)
- `load_messages` heals on read (_heal_orphaned_tool_calls,
  _strip_orphan_reasoning), so `persisted` != file bytes either

First divergence at index k stops the prefix; everything from k on gets
re-appended — records that are already in the file. Memory stays clean,
disk doubles. Fires on the interrupt path ("partial work kept"), which is
why heavy-^C sessions (like this one) show it most.

## Fix directions

1. Stop diffing by equality: the runtime knows how many records it has
   appended this process; persist the tail by count.
2. Or writer-side guard mirroring the loader's integrity rules: refuse to
   append a message whose tool_call id (or result tool_call_id) already
   exists in the file. Cheap set, kept per session.
3. Either way: the loader's IntegrityError proves the invariant is checkable
   — enforce it at write time, not just read time.

## Note

The 443-call storm was a separate, server-side event (llama.cpp parser).
This bug is js's own and predates today; the new strict loader from the
session-catalog port is what finally made it visible.
