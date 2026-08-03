# MODULARITY-PLAN.md — js as a lean core + bolt-on extensions

North star: js becomes the owner's langchain-done-right — every part independent,
bolt together what you want — modeled on pi's shape: a stupid-minimal substrate
plus ONE extension contract that everything workflow-shaped hangs off.

Ground truth used here: the js tree at this worktree (branch
`textual-repl-nonblocking`, ~36 modules in `js/`, ~18.9k lines incl. `js/toolkit/`)
and pi as actually installed at
`~/Repos/agents/little-coder/node_modules/@earendil-works/pi-coding-agent`
(v0.79.4 — note: there is no `node_modules/pi`; the package is
`@earendil-works/pi-coding-agent`, bin `pi`). Below, `PKG` = that package root.

---

## 1. Coupling map — where js actually stands

### 1.1 Fan-in: what everything depends on

From the import graph of `js/**/*.py` (AST-level, includes function-scoped imports):

| fan-in | module | notes |
|---|---|---|
| 13 | `js.toolkit.core` | correct — this is the tool contract, SHOULD be the hub |
| 10 | `js.providers` | cli, config, login_cli, logins, model_client, model_metadata, picker, routing, settings, tui |
| 8 | `js.logins` | cli, codex_auth, codex_provider, model_metadata, picker, providers, routing, tui |
| 7 | `js.codex_auth` | one provider's OAuth is a top-5 dependency of the whole tree |
| 7 | `js.colors`, `js.settings` | colors is a fine leaf; settings is config plumbing |
| 6 | `js.routing`, `js.config`, `js.sampling`, `js.toolkit.descriptions`, `js.toolkit.sanitize` | |
| 5 | `js.events`, `js.paths`, `js.model_metadata`, `js.toolkit.registry` | |

Fan-out champions: `js/cli.py` (9 internal deps), `js/toolkit/meta.py` (9),
`js/runtime.py` (7), `js/toolkit/wiki/__init__.py` (7).

### 1.2 The worst coupling offenders, ranked, with evidence

**#1 — `js/cli.py`: the 3336-line god module.** It is simultaneously:
arg parsing + mode dispatch (`main` at `js/cli.py:2815`), the REPL(s)
(`_repl_main` :2599, `_do_turn` :2453, `_turn_consumer` :2533), the entire
live-settings sync layer (~600 lines, `js/cli.py:267–930`: `_live_settings_overlay`
:633, `_sync_provider_from_live_settings` :837, …), transcript/debug sinks
(`_DebugTraceSink` :382, `_StdoutTee` :425), wiki mode (`_wiki_kickoff` :1917,
`_run_wiki` :1969), artifact mode (:2078, :2098), commit mode (:2211 plus
snapshot/backup helpers :2157–2210), bench (:1800), printonly (:2728),
offline compact (:2301), config migration (:1320), slash commands
(`_handle_command` :1343), and model/provider picker glue. Every feature must
tunnel through this file; `js/tui.py:305` reaches into it for a **private**
function (`from .cli import _sampling_for_turn`) — the view layer depends on
the god module, and cli imports tui back (`cli` ↔ `tui` cycle). It also does
import-time work: `_FULL_REGISTRY = build_default_registry()` at `js/cli.py:59`.

**#2 — the provider boundary does NOT hold.** CLAUDE.md says `js/model_client.py`
is "the ONE provider boundary… the only production module that import `ai`."
Reality — `import ai` (or SDK-internal imports) in **seven** production modules:

- `js/model_client.py:17-25` (legitimate),
- `js/runtime.py:22` — the loop's message representation IS the SDK type:
  `ai.messages.Message` (`js/runtime.py:330`), `ai.user_message` (:790),
  `ai.assistant_message` (:1132), raw `ai.types.messages.TextPart` construction
  (:1230), `ai.ProviderAPIError` (:182, :1086), `ai.ConfigurationError` etc. (:1105),
- `js/attach.py:13` — builds `ai.types.messages.FilePart` (:122, :242),
- `js/logins.py:17` — `ai.get_provider` (:504),
- `js/login_cli.py:10` — `ai.user_message` (:445),
- `js/providers.py:15` — `from ai.providers.base import _PROVIDER_REGISTRY`, a
  **private** SDK attribute (:464),
- `js/codex_provider.py:17-20` — a full provider implementation on SDK internals.

**#3 — `ToolContext` is a feature dumping ground** (`js/toolkit/core.py:58-88`).
The "mutable state shared across tool calls" carries wiki config
(`wiki_vault_lock_timeout_s` :71, `wiki_mode` :72, `wiki_no_archive` :73,
`vault_aliases` :74), artifact config (`artifact_dir/url/bin` :75-77), vision
(:78), fs read-before-write state (:79-83), todos (:83), and telemetry counters
(`last_prompt_tokens` … `last_incomplete_reason` :84-88). Plus dynamic attrs
bolted on from outside: `child_context.config = cfg` at `js/toolkit/meta.py:306`.
Every new feature grows this class → every feature change touches core.

**#4 — `Config` has the same disease** (`js/config.py:119-166`): mode-specific
fields in the core frozen dataclass — `wiki_vault_lock_timeout_s` :156,
`artifact_dir/url/bin` :160-162 (loaded at :390-392). And because Config is
duck-typed everywhere, there are **82** `getattr(cfg…/context…, …)` call sites
across the tree — the "typed loosely to avoid import cycles" comment on
`Telemetry.debug_log` (`js/runtime.py:198-199`) is the codebase admitting it.

**#5 — the tool registry is closed, not extensible.**
`js/toolkit/registry.py:13`: `from . import artifact, fs, meta, process_net, wiki`
— the assembler hardcodes every tool package; `build_default_registry`
(:142-146) concatenates `fs.tools() + process_net.tools() + meta.tools(flags) +
wiki.tools() + artifact.tools()`. Adding a tool pack = editing core. Tool
descriptions likewise resolve against exactly one directory
(`_DESCRIPTION_DIR = Path(__file__).with_name("tool_descriptions")`,
`js/toolkit/descriptions.py:38`).

**#6 — backwards edges from base tools into modes.**
`js/toolkit/fs.py:19`: `from .wiki.helpers import run` — the core fs tools import
a generic subprocess helper that lives inside the *wiki mode* package.
`js/cli.py:55` pulls `resolve_vault` from the same place. `wiki/helpers.py` is a
grab-bag: vault locking AND generic `run`/`read_text` AND terminal colors
(`from ... import colors as C`, `js/toolkit/wiki/helpers.py:14`).

**#7 — circular knots held together by function-scoped imports** (each of these
is a cycle the module structure forbids, papered over at call time):

| cycle | evidence |
|---|---|
| `toolkit.meta` ↔ `runtime` | `runtime.py:33` imports registry → `registry.py:13` imports meta → meta defers `from ..runtime import Telemetry, run_turn_async` at `js/toolkit/meta.py:223` (plus memory/persona/routing :221-225) |
| `cli` ↔ `tui` | cli imports tui; `js/tui.py:305` defers `from .cli import _sampling_for_turn` |
| `providers` ↔ `logins` | `logins.py:22` top-level; `providers.py:532,575` deferred |
| `providers` ↔ `model_metadata` | `model_metadata.py:30` top-level; `providers.py:484` deferred |
| `logins` ↔ `codex_provider` | codex_provider imports logins; `logins.py:495` deferred |
| `settings` → `providers` | `settings.py:305` deferred (value validation reaching up the stack) |

**#8 — `js/runtime.py` (1257 lines) mixes the loop with everything near it:**
the turn loop (`run_turn_async` :822), retry/backoff policy (:181-197), the whole
compaction engine (`compact_messages` :804, summarizer :775, pre-hook runner :726),
tool alias profiles (:39-127), pretty tool-arg rendering (:271-303), message
sanitization (:330), and a compat facade import that **builds the full default
registry at import time** (`from . import tools as T`, `js/runtime.py:26` →
`js/tools.py:8` `_REGISTRY = build_default_registry()`).

### 1.3 Boundary audit (the three claimed boundaries)

- **`model_client` = the one provider boundary: LEAKS** (see #2). The good news:
  the adaptation machinery already exists inside it (`history_to_ai_messages`
  `js/model_client.py:299`, `build_tool_result_messages` :426,
  `tool_specs_to_ai_tools` :226, `stream_model_async` :692) — the runtime just
  bypasses it for message construction and error taxonomy.
- **`routing` = the one route resolver: HOLDS.** `js/routing.py` is 190 lines,
  one entry (`resolve_model_route` :82), consumed by cli/config/model_client/
  runtime/meta/tui. Its only smell is reaching into the login store directly
  (`_saved_login` :65-79). This is what a boundary should look like — keep it.
- **`toolkit.registry` = assembles tools: HOLDS as a chokepoint, FAILS as a
  seam.** Everything does flow through `ToolRegistry` (`openai_specs()` :32 is
  the single model-facing renderer — good), but assembly is hardcoded (#5) and
  there is no way to register a tool from outside the package.

### 1.4 What is already clean (don't break it)

Genuine leaves with zero or trivial internal deps, usable standalone today:
`capped_process.py` (137), `events.py` (140 — the ircII ON-hook table),
`output.py` (86 — `OutputEvent`/`Sink`, **written but not yet wired**, its own
docstring: "Nothing here is wired into the runtime yet", `js/output.py:4`),
`sampling.py` (175), `memory.py` (234 — append-only JSONL sessions),
`transcript.py`, `supervisor.py` (101 — the async job table), `colors.py`,
`paths.py`, `stats.py`, `reasoning.py`, `toolkit/core.py` (minus the field
dumping ground), `toolkit/sanitize.py`. `setcmd.py` depends only on
events+settings. `persona.py` depends only on `promptexpand`, which depends on
`settings` for **one constant** (`js/promptexpand.py:50`, used at :93) — one
edge from being a standalone pair. `drain.py` already drives js **as a
subprocess** (`js/drain.py:248`) and touches the tree only at
`config.from_env`/`ToolContext`/`resolve_vault` (:44-48).

---

## 2. Target shape — pi's model translated to Python

### 2.1 What pi actually is (evidence)

- **Minimal core, stated and enforced.** "Pi is a minimal terminal coding
  harness" (`PKG/README.md:20`); by default the model gets **four tools**:
  read, write, edit, bash (`PKG/README.md:96`). The Philosophy section
  (`PKG/README.md:489-501`) lists what core deliberately does NOT do — no MCP
  (:493), **no sub-agents** (:495), no permission popups (:497), no plan mode
  (:499), **no built-in to-dos** (:501). Each is expected to be an extension.
- **Core contents** (`PKG/dist/core/`): `AgentSession` (loop; `prompt()`,
  `abort()`, `compact()`, `setModel()` — `core/agent-session.d.ts:163,328,404,459`),
  provider/model registry over the separate `@earendil-works/pi-ai` package
  (`ModelRegistry`, `core/model-registry.d.ts`), `SessionManager` + JSONL
  sessions + `core/compaction/`, the built-in tools (`core/tools/{read,write,
  edit,bash,grep,find,ls}.d.ts`), `SettingsManager`, the modes
  (`dist/modes/{interactive,rpc,print}`), and the extension host
  (`ExtensionRunner`, jiti loader).
- **ONE extension contract.** `export type ExtensionFactory =
  (pi: ExtensionAPI) => void | Promise<void>` (`core/extensions/types.d.ts:1029`).
  An extension is a module default-exporting that function. `ExtensionAPI`
  (`types.d.ts:808-963`) lets it: hook ~30 lifecycle events via `on()` (:809-838
  — `before_agent_start`, `context`, `tool_call` **with block**, `tool_result`
  **with rewrite**, `turn_start/end`, `session_*`, `model_select`, …);
  `registerTool` (:840) — where `ToolDefinition` (:335-366) carries
  `promptSnippet`/`promptGuidelines` (:343-345) so **a tool brings its own
  prompt fragments**; `registerCommand` (:842); `registerShortcut` (:844);
  `registerFlag` (:849) — extensions add CLI flags; `registerMessageRenderer`
  (:857); `registerProvider` (:946) with models, OAuth block, custom stream
  (:965-1027); plus session control (`sendMessage`/`appendEntry` :859-871) and a
  shared cross-extension `events: EventBus` (:962).
- **Discovery/lifecycle** (`core/extensions/loader.js`): project
  `.pi/extensions/` then global `~/.pi/agent/extensions/` then explicit paths
  (`discoverAndLoadExtensions` :443-480); a dir loads via `index.ts` or a
  `package.json` `"pi"` manifest (:373-401); TS runs through jiti with no build
  step (:264-274); activation is simply `await factory(api)` (:305). No
  deactivate export — teardown is the `session_shutdown` event
  (`types.d.ts:439-444`) plus `assertActive()`/`invalidate()` staleness
  (:1084-1086).
- **Proof it scales**: little-coder = "pi + 20 extensions + 30 skill markdown
  files… It doesn't fork pi or shadow its CLI — pi is a plain dependency"
  (`~/Repos/agents/little-coder/README.md:14`); "pi is the **minimal
  substrate** — agent loop, multi-provider API, TUI, session tree, compaction,
  extension model" (:12); key invariant: "Every little-coder mechanism ships as
  a pi extension that hooks pi's lifecycle events" (:397). 31 loadable
  extensions in `.pi/extensions/`, each ~1 file: `extra-tools/index.ts:9-135`
  registers glob/webfetch/websearch tools; `llama-cpp-provider/index.ts:71-76`
  registers providers; `permission-gate/index.ts:58` blocks `tool_call`s;
  `read-guard/index.ts:108-151` rewrites `tool_result`s; `turn-cap/index.ts:27-37`
  aborts the loop from `turn_start`; `clear-command/index.ts:17` registers a
  command; `knowledge-inject/index.ts:87` and `skill-inject/index.ts:191` return
  `{systemPrompt: …}` from `before_agent_start`; `subagent/index.ts:49-190`
  registers a `dispatch` tool — **sub-agents are an extension, exactly what pi
  core refuses to own.**

### 2.2 js lean core (what stays)

Translating pi's split onto the existing modules — core is what EVERY run needs:

| core concern | js modules (post-cleanup) | pi analog |
|---|---|---|
| agent loop | `runtime.py` (turn loop, dispatch, retry, cancel) | `AgentSession` |
| provider boundary | `model_client.py` + `routing.py` + `providers.py` + `logins.py` + `model_metadata.py` (as one cluster; the ONLY `import ai` zone) | `pi-ai` + `ModelRegistry` |
| sessions | `memory.py`, `transcript.py`, compaction (moved out of runtime.py into `compaction.py`, still core) | `SessionManager` + `core/compaction/` |
| tool dispatch | `toolkit/core.py`, `toolkit/registry.py` (registration-driven), `toolkit/descriptions.py` (multi-root) | tool host |
| default tool pack | `toolkit/fs.py`, `toolkit/process_net.py` | read/write/edit/bash/grep/find/ls in core (`PKG/README.md:96`) |
| config | `settings.py`, `config.py`, `setcmd.py` (+ namespaced ext config) | `SettingsManager` |
| events / extension host | `events.py`, `output.py`, **new `js/ext.py`** | `ExtensionRunner` + loader |
| prompt assembly | `persona.py`, `promptexpand.py` | system-prompt assembly + prompt templates |
| entry modes | `cli.py` (slimmed: arg parse, REPL, `-p`, `--login`) , `attach.py` (neutralized, §3 step 6) | `dist/modes/` stays core in pi too |
| plumbing leaves | `paths.py`, `colors.py`, `sampling.py`, `capped_process.py`, `reasoning.py`, `supervisor.py`, `tool_args.py`, `stats.py`, `picker.py` | — |

### 2.3 Extensions (what moves out)

Everything workflow-shaped, exactly like pi's exclusion list:

| extension | today's smear (why it must move) |
|---|---|
| **wiki** | `js/toolkit/wiki/` (5 modules), `cli.py:1917-2077` (+ `--wiki`/`--vault` flags), `config.py:156`, `toolkit/core.py:71-74`, `fs.py:19`, `drain.py` — one feature across 7+ core files |
| **artifact** | `js/toolkit/artifact/`, `cli.py:2078-2156`, `config.py:160-162`, `toolkit/core.py:75-77` |
| **drain** | `js/drain.py` + the `js-drain` entry point (`pyproject.toml [project.scripts]`) — already subprocess-driven |
| **commit agent** | `js/commit_helper.py`, `cli.py:2157-2300`, `prompts/commit/` |
| **subagents** (task tool + named-agent tools) | `toolkit/meta.py:191-556` + `registry.py:121-139`; pi core says "No sub-agents" (`PKG/README.md:495`), little-coder ships it as `subagent/index.ts` |
| **todos/plan/skill** | `toolkit/meta.py:30-100`; pi: "No built-in to-dos" (`PKG/README.md:501`) |
| **codex provider** | `js/codex_auth.py` + `js/codex_provider.py` (1255 lines for one provider, currently fan-in 7) — the `register_provider` exemplar |
| **tui** | `js/tui.py` — a view over the event stream |
| **bench / printonly** | `cli.py:1800-1916`, `cli.py:2683-2814` — command extensions |
| **irciipy windows** (future) | a view extension consuming `OutputEvent`s (`docs/nonblocking-windows.md`) — the extension model IS the delivery mechanism for this |

### 2.4 The js extension interface (pi's contract, in Python)

One new core module, `js/ext.py`. An extension is a **directory with `ext.py`**
(mirroring pi's dir-with-`index.ts`) exporting one function:

```python
# .js/extensions/wiki/ext.py
def activate(api: "ExtensionAPI") -> None: ...
```

Discovery, three layered roots — the SAME precedence rule persona already uses
for prompt dirs (repo `extensions/` < platform-config `extensions/` < project
`.js/extensions/`, later shadows earlier by dir name; cf. `registry.py:121-139`)
— plus installed packages via a `js.extensions` entry-point group for
pip-installable extensions. No manifest file needed; frontmatter-style
`ext.toml` optional later. Python needs no jiti — `importlib` on a path is the
whole loader. `--no-extensions` / `--extension PATH` flags mirror
little-coder's launcher (`bin/little-coder.mjs:111-126`).

```python
class ExtensionAPI:
    # -- registration (mirrors types.d.ts:840-960) --
    def register_tool(self, tool: Tool, *, descriptions_dir: Path | None = None,
                      prompt_snippet: str = "") -> None: ...
        # feeds registry assembly; descriptions_dir joins the description
        # search path (kills the single _DESCRIPTION_DIR, descriptions.py:38);
        # prompt_snippet == pi's ToolDefinition.promptSnippet (types.d.ts:343)
    def register_command(self, name: str, handler, *, help: str = "",
                         mode: bool = False) -> None: ...
        # slash command in the REPL; mode=True also exposes --<name> in argv
        # (this is how --wiki/--artifact/--commit/--bench leave cli.py)
    def register_flag(self, name: str, *, type=str, default=None) -> None: ...
    def register_provider(self, provider_def: ProviderDef, *,
                          stream_factory=None, oauth=None) -> None: ...
        # feeds providers.py's table (== pi registerProvider, types.d.ts:946)
    def register_prompt_dir(self, agent_id: str, path: Path) -> None: ...
        # an extension ships persona dirs (commit agent's prompts/commit/)
    # -- hooks (events.py grows the lifecycle set) --
    def on(self, event: str, handler: Callable[[dict], HookResult | None]) -> None: ...
        # events.py already has the table + dispatcher (events.py:69-137) and
        # canonical names (input/prompt/stream/tool_call/tool_result/response/
        # turn_start/turn_end/subagent/error/cancel/idle, events.py:9-23).
        # Add pi's load-bearing result semantics: tool_call may return
        # block(reason), tool_result may return replacement content,
        # before_agent_start may return extra system-prompt text
        # (pi types.d.ts:739-764). Handlers here are Python callables;
        # the ircII string-handler layer stays as the USER-scripting tier
        # on the same bus (two tiers, one event table).
    # -- config & state --
    def config(self, key: str, default=None): ...
        # reads ext.<name>.<key> from the merged settings tree; Config already
        # carries the raw merged dict (`settings`, config.py:146) — extensions
        # read through it instead of adding frozen fields to Config
    def state(self, context: ToolContext) -> dict: ...
        # returns context.ext[<name>] — a namespaced dict replacing the
        # per-feature fields on ToolContext (core.py:71-77)
    # -- doing things --
    def send_user_message(self, text: str) -> None: ...     # kickoff prompts
    def run_agent(self, *, agent_id, prompt, model=None, tools=None) -> str: ...
        # blessed path to run_turn_async on a child session — what the
        # subagents extension is built on (== little-coder subagent ext)
    events: EventHooks   # the shared bus, exposed (pi: `events: EventBus`, :962)
```

Lifecycle: `activate(api)` runs at startup after config, before registry
assembly; teardown = a `shutdown` event on the bus (pi's `session_shutdown`,
`types.d.ts:439-444`) — no deactivate export.

---

## 3. Staged extraction path

Rule for every step: `just test` (offline suite, 62 files) green and `just lint`
clean before the step is "done"; each step is separately committable via
`js --commit`. Steps are dependency-ordered; later steps consume seams built by
earlier ones.

### Step 0 — sever the backwards edges (small, zero-risk, do first)
**Moves:** (a) generic `run`/`read_text` out of `js/toolkit/wiki/helpers.py`
into `toolkit/core.py` (or a tiny `toolkit/proc.py`); re-point `fs.py:19`,
`wiki/ops.py:11`, `wiki/convert.py:7`. (b) `promptexpand.py:50` — take
`max_output_bytes` as a parameter/module constant instead of importing
`settings` (used once, :93). (c) hoist `_sampling_for_turn` out of `cli.py`
(into `sampling.py` or a small `turnprep.py`) so `tui.py:305` stops reaching
into cli private space. (d) delete the `js/tools.py` compat facade (37 lines,
"Compatibility facade" — CLAUDE.md forbids kept-alive compat) and give
`runtime.py:26` the registry it needs as a parameter — this also kills a
module-level `build_default_registry()` at import time (`tools.py:8`).
**Decouples:** fs←wiki, promptexpand←settings, tui←cli, runtime←tools.
**Verify:** `just test` (`test_fs_search`, `test_prompt_expansion`,
`test_nonblocking_repl`, `test_tool_runtime_smoke`), `just lint`.

### Step 1 — build the extension seam (THE leverage step; everything else rides it)
**Moves/creates:**
- `registry.py` becomes registration-driven: `build_default_registry` assembles
  from a list of registered tool sources instead of the hardcoded import at
  `registry.py:13`/`:143`. Core registers `fs` + `process_net` itself (they are
  pi's read/write/edit/bash analog); everything else arrives via the API.
- `descriptions.py` accepts a search-path of description dirs (kills the single
  `_DESCRIPTION_DIR`, :38).
- `ToolContext` gains `ext: dict[str, dict]` (namespaced per-extension state).
- `events.py` gains the missing lifecycle events (`before_agent_start`,
  `shutdown`) and a **Python-callable handler tier** with pi's result semantics
  (block / replace-content / extra-system-prompt) alongside the existing
  string-handler dispatcher.
- New `js/ext.py`: the `ExtensionAPI` above + the three-root discovery +
  `--extension/--no-extensions` flags.
- `cli.main` loads extensions once, after config resolution, before registry
  assembly; both module-level registry builds (`cli.py:59`, late `tools.py:8`)
  become explicit calls after ext loading.
**Interface exposed:** `js/ext.py` — the one contract.
**Decouples:** nothing yet by itself — it CREATES the seam.
**Verify:** whole suite + new `tests/test_ext_loader.py` (a toy extension
registering a tool, a command, a hook; assert precedence project>global>repo).
**Risk:** moderate — registry assembly order affects `test_meta_registry_cluster`,
`test_agent_tool_surface`, `test_tool_descriptions` expectations.

### Step 2 — wiki out (the exemplar; biggest smear, biggest proof)
**Moves:** `js/toolkit/wiki/` → `extensions/wiki/` (tools + prompts +
descriptions dir + vault helpers); `cli._wiki_kickoff`/`_run_wiki`
(`cli.py:1917-2077`) → `register_command("wiki", mode=True)` whose handler
seeds the kickoff prompt via `send_user_message`; `--vault` via
`register_flag`; `Config.wiki_vault_lock_timeout_s` (`config.py:156`) and
`ToolContext.wiki_*`/`vault_aliases` (`core.py:71-74`) → `api.config()` /
`api.state()`; `JS_WIKI_MODE`/`JS_WIKI_NO_ARCHIVE` env plumbing stays inside the
extension.
**Decouples:** cli, config, toolkit/core, fs (after step 0a) from wiki entirely.
**Verify:** `test_wiki_log_commit.py`, `test_meta_registry_cluster.py` (tool
counts shift — update expectations once), full suite. Run a live
`js --wiki=query` smoke by hand.
**Risk:** HIGH blast radius: wiki tools appear in default tool counts, drain
depends on `resolve_vault` (`drain.py:48`), and `fs.py` history. Do it right
after step 1 while the seam is fresh — it validates every API surface at once
(tools + command + flag + config + state).

### Step 3 — artifact out (the cheap repeat)
**Moves:** `js/toolkit/artifact/` → `extensions/artifact/`; `cli.py:2078-2156`;
`Config.artifact_*` (`config.py:160-162`) and `ToolContext.artifact_*`
(`core.py:75-77`) → ext config/state.
**Verify:** `test_artifact_native_tools.py`, suite.
**Risk:** low — smaller copy of step 2.

### Step 4 — subagents out; dissolve the meta↔runtime knot
**Moves:** `task` + fan-out machinery + `named_agent_tool` + prompt-dir agent
tools (`toolkit/meta.py:101-612`, `registry.py:121-139`) → `extensions/subagents/`,
built on `api.run_agent(...)` (the blessed wrapper over `run_turn_async` that
core exposes; child cfg/session derivation from `meta._agent_cfg` :191 moves
behind it). `todo/plan/skill` (`meta.py:30-100`) → `extensions/todos/` (tiny).
`toolkit/meta.py` ceases to exist.
**Decouples:** the deferred-import cycle at `meta.py:221-225`; registry no
longer imports meta; runtime's `_is_task_call`/fan-out special-casing
(`runtime.py:436-…`) becomes a generic "tool may fan out" capability flag on
`Tool` (the `is_fan_out_handler` probe, `meta.py:556`, becomes a Tool attribute).
**Verify:** `test_fan_out.py`, `test_fan_out_deadlock.py`,
`test_subagent_isolation.py`, `test_agent_model_selection.py`, suite.
**Risk:** HIGH — the fan-out/deadlock behavior is the subtlest code in the tree
(same-loop scheduling, `docs/nonblocking-windows.md`). Move it verbatim; the
only change is HOW it is wired, not what it does.

### Step 5 — modes out of cli.py (the god-module diet)
**Moves:** commit (`cli.py:2157-2300` + `commit_helper.py` + `prompts/commit/` →
`extensions/commit/`, registering the prompt dir + `--commit` mode command);
bench (`cli.py:1800-1916` → `extensions/bench/`); printonly (`cli.py:2683-2814`
→ `extensions/printonly/`); `tui.py` → `extensions/tui/` (a `--tui` mode command
that consumes the event stream). `drain.py` + its `js-drain` script entry →
`extensions/wiki/drain.py` (it is wiki-shaped: `drain.py:44-48`) with a thin
console-script shim, or its own dir — owner's call.
**cli.py residue:** arg parse, config assembly, REPL/`-p`, `--login`/model
listing, session flags. Target: cli.py under ~1200 lines; the live-settings
sync block (`cli.py:267-930`) survives but should migrate toward `setcmd`.
**Verify:** `test_commit_cli.py`, `test_commit_helper.py`, `test_bench_mode.py`,
`test_printonly.py`, `test_drain_*.py`, `test_cli_prompt_mode.py`, suite. Then
run `js --commit` itself — the commit agent must still work as dogfood.
**Risk:** moderate; mostly mechanical moves once `register_command(mode=True)`
exists, but `--commit` breakage blocks the whole workflow (CLAUDE.md), so land
commit LAST within this step and verify live immediately.

### Step 6 — enforce the provider boundary (highest blast radius — schedule alone)
**Moves/changes:**
- `runtime.py` drops `import ai` (:22): assistant-message construction
  (:790, :1132, :1230) and sanitization (:330) move behind `model_client`
  functions; the error taxonomy becomes js-owned exception types that
  `model_client` raises after catching SDK ones (`ProviderAPIError` handling at
  :182, :1086-1105 switches to `model_client.RetryableProviderError` etc.).
  History stays `list[dict]` — the conversion already lives at
  `model_client.history_to_ai_messages` (:299); the runtime just stops
  hand-building SDK parts.
- `attach.py` returns a neutral attachment dict; FilePart construction (:122,
  :242) moves into `model_client`.
- `providers.py:15` stops importing the SDK's private `_PROVIDER_REGISTRY`
  (:464) — replace with an explicit known-provider table or a capability probe
  routed through `model_client`.
- `codex_auth.py` + `codex_provider.py` → `extensions/codex/` registering via
  `api.register_provider` (mirrors `llama-cpp-provider/index.ts:71-76`). This
  dissolves the `logins↔codex_provider` cycle (`logins.py:495`) and drops
  codex's fan-in from 7 to 0.
- `login_cli.py:445`'s probe message and `logins.py:504`'s `ai.get_provider`
  route through `model_client`.
**Decouples:** the entire non-provider tree from the SDK; after this,
`grep -rn "import ai" js/` returns `model_client.py` only — the CLAUDE.md claim
becomes true, and swapping SDKs (or adding a second) is one module's problem.
**Verify:** `test_model_client.py`, `test_codex_oauth.py`,
`test_custom_login_routing.py`, `test_runtime_*`, `test_async_stream.py`,
`test_attach.py`, suite + a live `-p` smoke against the local llama.cpp route.
**Risk:** HIGHEST. Mid-stream message surgery (`_sanitize_assistant_message`,
incomplete-response repair :1117-1132) is behavior the offline suite covers only
partially. Mitigation: adapter-first — add the model_client functions, flip
runtime call sites one at a time, keep each flip a separate commit.

### Step 7 — event-first output; views become extensions
**Moves:** wire `OutputEvent` (`js/output.py`, currently unwired) through
`runtime`/`cli` per `docs/nonblocking-windows.md` ("Nothing in the turn path
writes to stdout"); default stdout sink reproduces today's bytes; `tui` and the
future irciipy-window layer subscribe as view extensions; `stats.py` becomes an
optional telemetry-sink extension. This is where the extension model and the
owner's nonblocking/windows north star converge: windows bind to event sources,
extensions produce/format events.
**Verify:** `test_output.py`, `test_transcript_log.py`, byte-for-byte transcript
comparison on a golden `-p` run.
**Risk:** moderate; behavior-preserving by construction (default sink).

### Leverage order, if only some of this happens
1 (seam) > 2 (wiki proves it) > 4 (subagents, kills the worst knot) >
6 (SDK boundary, kills the leak) > 5 (cli diet) > 3 (artifact) > 7 (events) —
step 0 is free and immediate.

---

## 4. The "independent part" test

"A part that can't be lifted out and used alone is not done." Assessment of
the biggest modules, today:

| module (lines) | liftable today? | blocker | minimal change to pass |
|---|---|---|---|
| `capped_process` (137), `events` (140), `output` (86), `sampling` (175), `memory` (234), `transcript` (222), `supervisor` (101), `paths`, `colors`, `stats`, `reasoning`, `toolkit/sanitize` | **YES** | none | — (protect this: no new internal imports) |
| `promptexpand` (329) | one edge away | `settings` import for one constant (:50, :93) | step 0b |
| `persona` (372) | with promptexpand | only imports promptexpand | lifts as a pair after 0b |
| `setcmd` (535) | with events+settings | clean deps | lifts as a trio |
| `settings` (718) | nearly | deferred `providers` import for value validation (:305) | inject a validator callable |
| `routing` (190) | with provider cluster | needs `providers` + login store (:75) | lifts inside the cluster |
| `providers`+`logins`+`model_metadata`+`codex_*` (~3000) | **NO** — 3 internal cycles (§1.2 #7) + private SDK poke (:15) | tangled cluster | after step 6: one `providers` package (registry+logins+metadata) with codex as a plug-in; lift as ONE part, not four |
| `model_client` (868) | with cluster | routing/sampling/reasoning/tool_args + `ai` | fine — it IS the SDK adapter; lifts with the provider cluster |
| `toolkit/fs` (941) | one edge away | `wiki.helpers.run` (:19) | step 0a; then fs+core+sanitize+descriptions lift as a "tools" part |
| `toolkit/process_net` (427) | with fs pack | fs + capped_process + core | lifts with the tool pack |
| `toolkit/meta` (639) | **NO** | deferred runtime/supervisor/config imports (:221-225), dynamic `context.config` (:306) | step 4 rebuilds it on `api.run_agent` |
| `runtime` (1257) | **NO** as standalone — but it's the core loop, its test is "usable with ONLY core parts" | `ai` (:22), `tools` facade (:26), colors, model_metadata | steps 0d + 6; compaction split into `compaction.py` makes both halves smaller than 700 lines |
| `wiki` / `artifact` packages | **NO** | ToolContext fields, cli wiring, colors | steps 2-3 make them the first true extensions — the proof of the whole plan |
| `drain` (459) | **almost** — drives js by subprocess (:248) | 3 imports (:44-48) | step 5; could be lifted today with ~20 lines of vendored helpers |
| `tui` (449) | one edge away | `cli._sampling_for_turn` (:305) | step 0c, fully clean after step 7 |
| `cli` (3336) | **NO — the anti-part** | it is the couplings | steps 2/3/5 shrink it; it never lifts, it dissolves |

---

## 5. What NOT to do

- **Don't split into pip packages yet.** pi is one npm package with an internal
  monorepo split; little-coder consumes it whole. The win is the SEAM, not the
  packaging. Directory moves inside one repo (`js/` core, `extensions/` in-repo
  defaults) keep `just test` and `js --commit` trivial. Package extraction is a
  later, cheap follow-on once imports are one-directional.
- **Don't invent a plugin manifest format.** pi's contract is "a function that
  gets an API object" — `ext.py` with `activate(api)` is the whole spec. Config
  rides the existing jsrc `set ext.<name>.<key> <value>` lines; no new file
  formats.
- **Don't move fs/process_net out of core.** pi ships read/write/edit/bash in
  core (`PKG/README.md:96`); a harness with no hands is not minimal, it's inert.
- **Don't touch the fan-out concurrency logic while moving it** (step 4) — the
  same-loop scheduling contract in `docs/nonblocking-windows.md` is
  load-bearing; relocation and rework are separate commits.
- **Keep `routing.py` as the shape template.** It already IS what every boundary
  here should become: one dataclass out, one entry function, docstring stating
  what it replaced (`routing.py:1-11`).
