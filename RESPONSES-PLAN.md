# js Responses-first: what is left to build

WHAT THAT WEIRD DIRECTORY IS: ~/js-responses-first-beefup
is not a js branch or a fork. It is a build machine. It holds
pipeline.py, which takes a spec file and produces a merged branch by
running a planner, then parallel coder-plus-reviewer lanes in their own
git worktrees, then a final gate. It also still holds this Responses
task list, which is where its name came from. "beefup" was an agent's
word, not yours. Its own STATE.md explains the rest.

Your original Responses-first list, minus everything that has already
shipped. Everything below is unimplemented as of 2026-07-27. Base for
all of it: main at f4b688c, which already contains full MCP client
support, lazy tool discovery, and the metadata-only skill catalog.

Long-form slice specs, if you ever want them:
~/js-responses-first-beefup/slices

To run any of this through the pipeline:

    cd ~/js-responses-first-beefup
    ./pipeline.py specs/NAME.md --name RUN --source-repo ~/js

## Wave A — nothing blocks these, they can run at once

### Canonical Responses transcript (slice 01)

Typed representation for Responses items: message, reasoning,
function and custom call plus output, tool-search call and output,
compaction, and an opaque unknown variant. Preserve for replay: type,
item id, call_id, caller, namespace, encrypted reasoning content,
status, and unrecognized fields. The Codex provider surfaces these
through a compatibility view so the existing runtime API keeps
working. Stateless store:false history must replay in original order,
and old session files must load without migration.

Acceptance: round-trip tests keep every replay field; unknown item
types survive serialize, load and replay; mixed reasoning, message
and tool-call ordering is preserved; existing Codex OAuth and runtime
integration tests still pass; non-Responses providers unchanged.

Checks: pytest tests/test_codex_oauth.py tests/test_model_client.py
tests/test_memory.py tests/test_runtime_offline_integration.py

### Digest-based read-before-edit (slice 04)

On a successful read, record a ToolContext-scoped observation:
resolved path, SHA-256, size, mtime-ns, and device/inode where
available. Before patch, overwrite, undo and remove,
re-read and reject when the target changed. Distinguish symlink path
identity from the resolved target so a swapped symlink is caught.
Refresh observations after a successful mutation. Observations never
cross sessions, subagents, worktrees or unrelated ToolContexts. Exact
match patch semantics stay; this is an added stale-write defense with
a precise model-facing error telling it to re-read.

Acceptance: read then edit succeeds; read, external mutation, then
edit fails without touching the file; a same-size same-mtime byte
change is still caught by digest; atomic replace and symlink swap are
covered; a child agent's read cannot authorize a parent write.

Checks: pytest tests/test_tool_runtime_smoke.py tests/test_fs_search.py
tests/test_subagent_isolation.py tests/test_runtime_offline_integration.py

### Parallel-safety metadata and concurrent dispatch (slice 03, part 1)

Immutable per-tool metadata for parallel safety and side-effect
scope; every existing tool defaults to exclusive. Mark only proven
read-only operations parallel-safe to start: read and fs_search.
Leave fetch exclusive until its save and write behavior
is split. Add a shared/exclusive admission gate: safe calls may
overlap, an exclusive call waits for active readers, runs alone, and
blocks later readers. Preserve the model's call order in appended
results regardless of completion order, propagate cancellation, and
give every call id exactly one result or error record. Add a
configurable direct-tool concurrency ceiling. Existing fan-out
deadlock protection must survive.

Acceptance: timing tests prove six independent safe reads overlap;
write, patch, shell and terminal calls never overlap anything; a
mixed batch respects model order and writer barriers; failed or
cancelled batches keep all call and result pairs.

Checks: pytest tests/test_fan_out.py tests/test_fan_out_deadlock.py
tests/test_tool_runtime_smoke.py tests/test_async_stream.py
tests/test_runtime_offline_integration.py

### Typed multimodal tool results (slice 05, part 1)

A ToolResult union covering text, image, audio, file reference,
structured JSON, error state and metadata. Handlers may return typed
results while a text compatibility adapter keeps every existing
caller working. Enforce byte and token caps across mixed content, and
validate MIME from bytes rather than file extension. Upgrade local
image read and terminal snapshots first.

Acceptance: text tool behavior is unchanged to callers; invalid or
oversized media fails safely; attachment, terminal and runtime tests
pass.

Checks: pytest tests/test_attach.py tests/test_interactive_tools.py
tests/test_tool_runtime_smoke.py

## Wave B — needs slice 01 merged first

### Session state, capabilities and sticky turn routing (slice 02)

Capture response headers from Codex HTTP streaming, especially
x-codex-turn-state, and resend the captured value on every
continuation request inside that model and tool turn. Never send it
into the next user turn, another provider, another model route or
another session. Add structured model capability metadata parsed from
the codex models endpoint, retaining unknown fields conservatively:
parallel tool calls, custom tools, tool search and deferred tools,
namespaces, multimodal results, WebSocket or code mode where
advertised. Conservative defaults with an explicit override and probe
seam; never infer support from a model-name substring. Token refresh
and retry paths must not drop or cross-contaminate turn state.

Acceptance: a simulated response header is echoed exactly on the
tool-result continuation; the following user turn and any route
change omit it; concurrent sessions cannot see each other's state;
model listing keeps capability fields while still returning usable
ids; debug output redacts auth and does not print turn state.

Checks: pytest tests/test_codex_oauth.py tests/test_picker.py
tests/test_model_metadata.py tests/test_async_stream.py
tests/test_runtime_offline_integration.py

### Multimodal wire encoding (slice 05, part 2 — needs 01 and 02)

Encode native function_call_output and custom_tool_call_output
content arrays where the negotiated capability supports it, with
fallbacks for transports that cannot carry media in tool results.
Durable session history dehydrates large media to controlled assets
or references so replay does not re-embed base64, while the current
turn may still carry native bytes.

Acceptance: a PNG read reaches a simulated Responses continuation as
a real image item; unsupported providers receive a useful path or
text fallback rather than malformed wire data; replay does not
repeatedly embed large payloads.

Checks: pytest tests/test_attach.py tests/test_codex_oauth.py
tests/test_model_client.py tests/test_memory.py

### Finish deferred tools on the wire (rest of slice 06 — needs 01 and 02)

Lazy discovery, the tool_discovery catalog and the skill catalog
already shipped in the MCP work. What remains: emit defer_loading
true only when the negotiated capability supports it, keeping the
client-executed compatibility path otherwise; add a canonical
tool-search call and output item that survives stateless replay using
slice 01's preservation; and actually measure the prompt-token
reduction for the default large surface, before and after, rather
than assuming it.

Checks: pytest tests/test_agent_tool_surface.py
tests/test_tool_descriptions.py tests/test_context_budget.py
tests/test_meta_registry_cluster.py

### parallel_tool_calls wiring (slice 03, part 2 — needs 02 and 03a)

Set parallel_tool_calls true only when tools exist and the target
capability permits it. Codex OAuth Sol, Terra, Luna and 5.5 may use a
conservative verified override until the metadata advertises it.
Request-body tests assert its presence and its absence.

## Wave C — after everything above

### Typed lifecycle and policy hooks (slice 07)

Typed contributors for session, turn, request, response, tool,
compaction and subagent stages: session start and end, turn start and
end, request prepare and sent, response item, tool discovered, call,
pre, start, output, post and abort, pre and post compact, subagent
start and end. Typed pre-tool decisions: allow, deny, argument
rewrite, require approval, message injection. Deterministic
contributor ordering, timeouts, cancellation, and an explicit
fail-open or fail-closed policy. No arbitrary transcript mutation —
changes pass through typed decisions and stay auditable. Async hooks
for one safe call must not serialize unrelated safe calls. Redact
credentials and opaque routing state unless a contributor holds an
internal capability. Existing EventHooks and the /on command adapt
over the new lifecycle without breaking current scripts. Include a
minimal approval-decision seam; no interactive permission UI.

Acceptance: allow, veto, rewrite, timeout, exception, cancellation
and ordering are all tested; hook effects appear in telemetry without
exposing secrets; parallel tools keep concurrency under nonblocking
hooks; tool abort and subagent lifecycle fire exactly once; existing
/on, compaction, runtime and supervisor tests pass.

Checks: pytest tests/test_ircii_loader_events.py
tests/test_repl_harness.py tests/test_supervisor.py
tests/test_subagent_isolation.py tests/test_fan_out.py
tests/test_memory.py tests/test_runtime_offline_integration.py

## Final gate for any of this

    git diff --check BASE..HEAD
    just lint
    just test

## Non-goals across all of it

No WebSocket transport. No Code Mode. No interactive permission UI.
