# MCP Implementation Plan

Plan for adding Model Context Protocol support to `js`. Written against
specification revision **2025-11-25**, pulled from modelcontextprotocol.io on
2026-07-26. Nothing here is inferred from another implementation.

## Pinned Spec Facts

Everything in this section is a direct requirement. Ignoring any of it produces
a client that fails against conformant servers.

### Base protocol

- JSON-RPC 2.0, UTF-8. Three message kinds: request, response, notification.
- Request `id` MUST be a string or integer and MUST NOT be `null` (this differs
  from base JSON-RPC). An `id` MUST NOT be reused within a session.
- Notifications MUST NOT carry an `id`, and the receiver MUST NOT reply.
- Schemas default to JSON Schema **2020-12** when no `$schema` is present.
  Implementations MUST support 2020-12 and MUST fail gracefully on dialects
  they do not support.
- `_meta` is reserved. Any prefix whose second label is `modelcontextprotocol`
  or `mcp` belongs to the protocol.

### Lifecycle

1. Client sends `initialize` with `protocolVersion`, `capabilities`,
   `clientInfo` (`name`, `version`, optionally `title`, `description`, `icons`,
   `websiteUrl`).
2. Server replies with `protocolVersion`, `capabilities`, `serverInfo`, and an
   optional `instructions` string.
3. Client sends the `notifications/initialized` notification.

Before the server answers `initialize`, the client sends nothing but `ping`.
Before the server receives `initialized`, it sends nothing but `ping` and
logging.

Version negotiation: the client sends its latest supported version; a server
that does not support it replies with a version it does support; a client that
cannot accept that reply disconnects.

Shutdown has no protocol message. For stdio: close the server's stdin, wait,
then `SIGTERM`, then `SIGKILL`.

Capability keys — client: `roots`, `sampling`, `elicitation`, `tasks`,
`experimental`. Server: `prompts`, `resources`, `tools`, `logging`,
`completions`, `tasks`, `experimental`. Sub-capabilities: `listChanged` on
prompts/resources/tools, `subscribe` on resources.

### Transports

**stdio.** The client launches the server as a subprocess. Messages are
newline-delimited and MUST NOT contain embedded newlines. The server MUST NOT
write anything to stdout that is not a valid MCP message; it MAY write anything
at all to stderr, and the client MUST NOT treat stderr output as an error
signal. Clients SHOULD support stdio wherever possible.

**Streamable HTTP.** One endpoint path serving both POST and GET.

- Client POSTs every message, with an `Accept` header listing both
  `application/json` and `text/event-stream`.
- A POSTed notification or response gets `202 Accepted` with no body.
- A POSTed request gets either `application/json` (one object) or
  `text/event-stream` (an SSE stream). The client MUST handle both.
- `MCP-Session-Id`: if the server returns this header on the `InitializeResult`,
  the client MUST echo it on every later request. A `404` in reply to a request
  carrying a session id means the session is gone and the client MUST start a
  new one with a fresh `InitializeRequest`. `DELETE` ends a session.
- `MCP-Protocol-Version`: the client MUST send the negotiated version on every
  request after initialization. A server that receives an unsupported value MUST
  answer `400`. A server that receives none assumes `2025-03-26`.
- Resumption is via HTTP GET with `Last-Event-ID`, always — never via POST.
- Server-side security: validate `Origin` (403 when present and invalid), bind
  to 127.0.0.1 when local, authenticate. These exist to stop DNS rebinding.

The 2024-11-05 HTTP+SSE transport is deprecated. The fallback probe is: POST an
`InitializeRequest`; on 400/404/405, GET the same URL and expect an `endpoint`
event.

### Tools

`tools/list` takes an optional `cursor` and returns `tools` plus an optional
`nextCursor`. Pagination is real and MUST be followed to the end.

A `Tool` carries `name`, optional `title`, `description`, `inputSchema`
(required, a valid JSON Schema object, never `null`), optional `outputSchema`,
optional `annotations`, optional `icons`, and optional
`execution.taskSupport` (`forbidden` by default).

Tool names should stay within `[A-Za-z0-9_.-]`, 1–128 characters, and are
case-sensitive.

`tools/call` takes `name` and `arguments` and returns `content` (an array of
`text` / `image` / `audio` / `resource_link` / `resource` blocks), optional
`structuredContent`, and `isError`.

The error split is the part most clients get wrong:

- **Protocol errors** (unknown tool, malformed request) are JSON-RPC errors.
- **Execution errors** (the API failed, the input was out of range) come back as
  a normal result with `isError: true` and text the model can read and correct
  against.

A client that turns execution errors into exceptions destroys the model's
ability to self-correct. Both kinds must reach the model as text.

`notifications/tools/list_changed` fires when the tool set changes, if the
server declared `listChanged`.

### Client features worth knowing about now

`sampling/createMessage` lets a server borrow the client's model. Parameters:
`messages`, `modelPreferences` (`hints` as loose substrings, plus
`costPriority` / `speedPriority` / `intelligencePriority` in 0–1), `systemPrompt`,
`maxTokens`, and optionally `tools` + `toolChoice`. The result is a single
assistant message with `model` and `stopReason`. The spec is emphatic that a
human should be able to deny it. Rejection is error code `-1`.

`roots` tells a server which directories it may work in. `elicitation` lets a
server ask the user a question mid-call.

## The Draft Revision Changes The Shape

`2025-11-25` is the current revision, but a **draft** is in review and it is not
an incremental one. It deletes several things this plan would otherwise build.
No date has been assigned to it yet, and no SDK implements it. Checked
2026-07-26.

What the draft removes:

- **The `initialize` / `notifications/initialized` handshake.** MCP becomes
  stateless. Every request carries its own protocol version and client
  capabilities in `_meta`, under `io.modelcontextprotocol/protocolVersion` and
  `io.modelcontextprotocol/clientCapabilities`. A new `server/discover` RPC,
  which servers MUST implement, replaces the handshake for up-front version
  selection.
- **`Mcp-Session-Id` and protocol-level sessions.** Servers that need
  cross-call state mint their own handles and pass them as ordinary tool
  arguments.
- **SSE resumability.** `Last-Event-ID` and SSE event ids are gone; a broken
  stream loses the in-flight request and the client re-issues it under a new id.
- **The HTTP GET endpoint and `resources/subscribe`**, replaced by a single
  long-lived `subscriptions/listen` POST stream that clients opt into per
  notification type.
- **`ping`, `logging/setLevel`, `notifications/roots/list_changed`.**
- **Server-initiated requests.** `roots/list`, `sampling/createMessage`, and
  `elicitation/create` are replaced by the Multi Round-Trip Request pattern: the
  server returns an `InputRequiredResult` carrying `inputRequests`, and the
  client retries the original request with `inputResponses` attached. Every
  result now carries a required `resultType` of `"complete"` or
  `"input_required"`.

What it deprecates outright: **Roots, Sampling, and Logging**. The stated
migrations are to pass directories through tool parameters or server config, to
call the LLM provider API directly instead of borrowing the client's model, and
to log to stderr or OpenTelemetry. Deprecated features keep working for at least
twelve months under the new feature-lifecycle policy.

What it adds that matters here: `inputSchema` and `outputSchema` loosen to allow
any 2020-12 keyword (which is more argument for `raw_schema`), list results gain
required `ttlMs` and `cacheScope` fields for client-side caching, `tools/list`
should return a deterministic order to help prompt caching, `Mcp-Method` and
`Mcp-Name` headers become required on Streamable HTTP POSTs, and `tasks` moves
out of core into an extension under a new opt-in extensions framework.

**What this means for the build.** Target `2025-11-25` — it is what every SDK
speaks today. But do not sink effort into the parts the draft deletes: HTTP
session-id plumbing, SSE resumption, and anything built on server-initiated
requests. The stdio path, `tools/list`, `tools/call`, and the result-translation
work are all unaffected and survive both revisions.

## Scope

Two directions exist and only one of them is worth building first.

**Phase A — `js` as MCP client.** `js` connects to servers and their tools join
the registry. This is the whole payoff: every MCP server ever written becomes a
`js` tool set without writing a line of tool code.

**Phase B — `js` as MCP server.** The `js` toolkit (fs, terminal, search,
browser, wiki, artifact) exposed over stdio so Claude Code or any other host can
drive it. Small once Phase A exists, and worth doing, but it delivers nothing to
the owner's own harness.

Deliberately out of scope for the first pass: the `tasks` capability,
`completions`, resource `subscribe`, and the OAuth authorization framework. Add
authorization only when a specific remote server demands it; stdio servers take
credentials from the environment and the spec says so explicitly.

## Dependency Decision

`mcp` on PyPI: **1.28.1** is the stable line; 2.0.0a1/b1 is alpha with a
different `Client` API. v1 is documented as recommended for production and in
maintenance mode. Requires Python 3.10+, and `js` is already 3.12+.

The v1 client API, verified against the v1.28.1 docs:

```python
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

async with stdio_client(StdioServerParameters(command="uvx", args=["some-server"])) as (read, write):
    async with ClientSession(read, write, sampling_callback=...) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("add", arguments={"a": 5, "b": 3})
```

The streamable HTTP client yields a third value, the session id accessor.

**Recommendation: take the dependency, pinned to `mcp>=1.28,<2`.** It drags in
httpx, pydantic, anyio, and starlette. Pydantic and anyio are the ones to be
aware of — `js` currently has neither. The alternative is hand-rolling stdio
JSON-RPC, which is genuinely about 200 lines for the happy path and gives up
nothing on day one, because stdio needs no HTTP stack at all.

The case for the dependency is not the framing code. It is that the SDK already
tracks version negotiation, the pagination cursor, the SSE resumption rules, and
the `2025-03-26` fallback assumption, and those are the parts that rot with every
spec revision. The case against is that `js` has ten dependencies today and each
one is a thing that breaks `just sync`.

If the answer is no dependency, hand-roll stdio only, and revisit at Phase 2 —
Streamable HTTP with resumable SSE is where hand-rolling stops being cheap.

## The Async Boundary

This is the one architectural problem, and it needs settling before any code.

`js` tool handlers are synchronous: `Handler = Callable[..., str]` in
`js/toolkit/core.py`. The runtime is async at the top (`run_turn_async`, with
`asyncio.run` at `js/runtime.py:1590`) and pushes handlers into
`ThreadPoolExecutor`s, awaiting them through `asyncio.wrap_future`. So when a
tool runs, an event loop is alive on the main thread and the handler is on a
worker thread.

The MCP SDK is anyio-async throughout, and a stdio session is a long-lived
subprocess that must outlive any single turn.

**Design: one `McpHub` singleton owning a daemon thread with its own event
loop.** Every session lives on that loop. The bridge handler stays a plain sync
function and does:

```python
future = asyncio.run_coroutine_threadsafe(session.call_tool(name, args), hub.loop)
result = future.result(timeout=cfg.mcp_call_timeout_s)
```

This keeps the `Tool` contract untouched, requires no runtime surgery, works
identically when a tool is called outside the runtime (tests, commit-agent), and
gives sessions an owner whose lifetime is the process rather than the turn.

Rejected alternative: running sessions on the runtime's own loop. It is fewer
moving parts, but it ties session lifetime to a turn and breaks every non-runtime
caller.

## Mapping MCP Onto `js` Types

### Names

An MCP tool name collides with a `js` tool name sooner or later, and
`ToolRegistry` builds a lowercase alias table where a collision silently wins or
loses. Namespace on import: `<server>.<tool>` (dots are legal in MCP tool names
and in `js` selectors). `registry.select()` already globs, so
`--tools 'jupyter.*'` works for free, and so does excluding one server.

Reject or mangle imported names outside `[A-Za-z0-9_.-]` before they reach the
alias table.

### Schemas — this needs a change to `core.Tool`

`Tool.openai_spec()` hardcodes the wrapper:

```python
"parameters": {"type": "object", "properties": self.params,
               "required": list(self.required), "additionalProperties": False}
```

An MCP `inputSchema` is an arbitrary 2020-12 document. It may set
`additionalProperties: true`, use `$ref`/`$defs`, `anyOf`, or nested objects,
and flattening it into `params` loses all of that. Rewriting it loses more.

Add an optional `raw_schema: dict | None` to `Tool`; when set, `openai_spec()`
emits it verbatim as `parameters` and ignores `params`/`required`. This is a
small, contained change to a frozen dataclass and it keeps every existing tool
byte-identical.

Watch for providers that reject schema features the OpenAI function-call
surface does not accept. That is a translation layer at the provider boundary,
not at the MCP boundary, and it should stay there.

### Results

`CallToolResult.content` is a list; the handler must return one `str`.

- `text` blocks: join on newline.
- `structuredContent`: when present, prefer it, serialized as JSON. The spec
  says servers that return it should also put the JSON in a text block, so
  naive concatenation duplicates the payload.
- `isError: true`: return the text with the existing `ERROR: ` convention so
  the model self-corrects, and never raise.
- `image` / `audio`: base64 payloads that will wreck a context window if pasted.
  Either drop them with a one-line note or route images through the existing
  vision path when `vision_enabled`. Decide before Phase 1 ships.
- `resource_link` / `resource`: render the URI and mime type, fetch on demand.

Everything must pass through the existing `max_tool_result_bytes` cap. A remote
tool is exactly the kind of thing that returns 4 MB.

### Untrusted text

Tool descriptions and annotations come from a third party and land in the
model's tool list, which is a prompt-injection path the spec itself flags. Every
description already funnels through `render_tool_name_sections` in
`registry.openai_specs()` — that is the chokepoint. Strip `{{...}}` template
syntax from imported descriptions there so a server cannot inject directives
into the co-present-tool blocks, and cap description length.

## Configuration

Servers belong in the existing config file, merged across global and project
scope like everything else:

```toml
[mcp.servers.jupyter]
command = "uvx"
args = ["mcp-jupyter-notebook"]
env = { MCP_JUPYTER_SESSION_MODE = "server" }
cwd = "~/notebooks"
enabled = true
tools = ["notebook_*"]        # allow-list, globbed, optional

[mcp.servers.linear]
url = "https://mcp.linear.app/mcp"
headers = { Authorization = "Bearer ..." }
```

Plus a few `Config` fields: `mcp_connect_timeout_s`, `mcp_call_timeout_s`,
`mcp_enabled`.

**Connect eagerly, fail softly.** The model cannot call a tool it cannot see, so
`tools/list` has to happen before the registry is built. That puts every
server's startup latency on the critical path of every `js` invocation. So:
connect with a short timeout, in parallel, and let a server that fails to start
log one line to stderr and drop out of the registry. A broken server must never
prevent `js` from starting. If aggregate latency becomes annoying, cache the
tool list per server keyed on command+args and refresh on `list_changed`.

The per-server allow-list is not optional. One server with sixty tools will eat
the tool budget and degrade selection for every other tool.

## Phases

**Phase 0 — spike.** One throwaway script: stdio to a known server, initialize,
`tools/list`, one `tools/call`. Confirms the SDK's shape and the async bridge
before anything lands in `js/`.

**Phase 1 — stdio client.** New `js/toolkit/mcp.py` (hub, session lifecycle,
bridge tool construction) plus config keys, `raw_schema` on `Tool`, and
registry integration in `build_default_registry`. Tests against a fake server
shipped in `tests/`.

**Phase 2 — Streamable HTTP.** Only when a remote server is actually wanted;
stdio covers every locally-run server. Implement the `MCP-Protocol-Version`
header and the `MCP-Session-Id` echo because today's servers require them, but
keep both behind one thin seam — the draft deletes them. Skip `Last-Event-ID`
resumption and the deprecated-transport probe unless something concrete needs
them; the first is being removed and the second predates two revisions.

**Phase 3 — client features `js` offers back.** Read the draft before starting
this phase. `roots` and `sampling` are both deprecated there, and the migration
the spec suggests for sampling is exactly what `js` already does: call the
provider directly. Building sampling now means building a feature with a
twelve-month clock on it, and handing a third-party server the owner's model
budget. `elicitation` survives but its shape changes completely under Multi
Round-Trip Requests, so anything written against the current server-initiated
form is throwaway. The honest recommendation is to skip this phase entirely
unless a specific server refuses to work without it.

**Phase 4 — resources and prompts.** Expose `resources/read` as a `js` tool and
map server prompts onto the existing inline-directive machinery.

**Phase 5 — `js` as MCP server.** stdio, exposing the toolkit. The output must
be clean: nothing but framed JSON on stdout, all logging to stderr. Given how
much of `js` prints, audit that before trusting it.

**Phase 6 — authorization.** Only when a remote server requires OAuth.

## Failure Modes To Design For

These are the ones that will actually happen:

- A server writes a banner to stdout, violating the spec, and framing breaks on
  the first read. Fail that server, keep the others.
- A server hangs during `initialize`. Per-server connect timeout, enforced.
- A server dies mid-session. The next call must return `ERROR: ` text, not
  raise into the runtime. Decide whether to reconnect or stay dead for the
  process lifetime; staying dead is simpler and honest.
- `notifications/tools/list_changed` arrives while `ToolRegistry` is a frozen
  dataclass held by the runtime. Either rebuild between turns or ignore the
  notification and document that a restart is required. Ignoring it is a
  legitimate first answer.
- A tool call outlives the turn. The call timeout must be shorter than whatever
  patience the owner has, and cancellation should send a `CancelledNotification`
  rather than just abandoning the future.

## Testing

`just test` is the offline suite, so the fake server must be local: a
`tests/fake_mcp_server.py` launched as a real subprocess over stdio, which
exercises the actual framing rather than a mock. Give it a tool that succeeds,
one that returns `isError: true`, one that returns `structuredContent`, one that
returns 10 MB, one that hangs, and a paginated `tools/list` with a `nextCursor`.

Worth asserting directly: `id` is never reused within a session; `initialized`
is sent before any other request; the negotiated version is echoed on HTTP
requests; pagination runs to exhaustion; `isError` reaches the model as text.

## Decisions Needed

1. Take the `mcp` SDK dependency, or hand-roll stdio and revisit at Phase 2?
2. Namespace imported tools as `<server>.<tool>`, or something shorter?
3. Image and audio content blocks: drop, or route through the vision path?
4. `list_changed`: rebuild the registry between turns, or require a restart?
5. Does Phase B (`js` as a server) matter, or is this client-only?
6. Is Streamable HTTP wanted at all in the first pass, or is stdio enough until
   a remote server actually shows up?

Decision 5 from the first draft of this plan — whether a third-party server
should be able to spend the owner's tokens through sampling — is answered by the
spec itself. Sampling is deprecated in the draft revision and the suggested
migration is to call the provider API directly. Don't build it.
