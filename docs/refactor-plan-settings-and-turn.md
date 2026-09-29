# Refactor plan: settings projection and splitting `run_turn_async`

> Plan written 2026-09-29 by a read-only planning agent. Line numbers are anchors to re-grep, not addresses.
> Naming: js retired the word "knob" in operator-facing text (js-jwc.4); when implementing, name the module and
> row type after settings (e.g. `js/turn_settings.py`, `TurnSetting`), not knobs.


I used the `codebase-design` vocabulary throughout: interface, depth, seam, adapter, locality, and the deletion test.

## 0. Re-baseline before starting (do this before either part)

Line numbers already moved once while I was reading. `main` went from `2193857` to `31900da` when bead/picker merged, and cli.py's `_LIVE_LIMIT_FIELDS` moved from ~448 in the review to 488 and then 490. Treat every number in this plan as an anchor to re-grep, not an address.

In-flight work that collides with this plan (`bd list`):
- **js-1g1.13 resilience** (bead/resilience): rewrites the retry loop inside `run_turn_async` and adds settings (retry budget, `runtime.stream_idle_seconds`).
- **js-1g1.14 compaction** (bead/compaction): rewrites `_maybe_compact_request_for_budget` and adds a failure breaker whose state crosses turns.
- **js-1g1.12 reasoning** (bead/reasoning): changes the reasoning stream and the shape of the assistant record.
- **js-1g1.15 edits, 1g1.26 shell head+tail, 1g1.27–30 (LSP, notebook, keys, paste)**: each probably adds a setting, which today means touching all six places.

Right now every bead branch is 0 commits ahead and has a clean worktree, so all of this work is still to come. **Part 1 starts after those beads merge. Part 2 starts only after resilience, compaction and reasoning merge.**

Re-baseline commands (all read-only):
```
git -C /home/ronald_rump/js log --oneline -1
cd /home/ronald_rump/js
grep -n "_LIVE_[A-Z_]* *:\|^def _cfg_for_live_state\|^def _live_" js/cli.py
grep -n "_int_knob(\|_jsrc_field(\|_numeric_setting(\|^class Config\|^def from_env" js/config.py
grep -n "active_context\.[a-z_]* = getattr(cfg\|_settings.knob(live_settings" js/runtime.py
grep -n "^class ToolContext\|= _knob(" js/toolkit/core.py
grep -n "^_INHERITED_FIELDS\|^def _child_context" js/toolkit/meta.py
grep -n "^async def run_turn_async\|^def run_turn(\|^    def \|^    async def \|^        def \|^        async def \|nonlocal" js/runtime.py
awk '/^async def run_turn_async/{s=NR} /^def run_turn\(/{print NR-s " lines"}' js/runtime.py
grep -rn 'runtime, "_backoff"\|runtime, "_resolve_context_window"\|runtime, "_dispatch_batch"\|runtime.sys, ' tests
```
Inventory diff. It is read-only and prints the fields that differ between the four hand-kept lists:
```
uv run python -c "
import dataclasses, js.config as c, js.toolkit.core as k, js.toolkit.meta as m, js.cli as cli
cfg={f.name for f in dataclasses.fields(c.Config)}; ctx={f.name for f in dataclasses.fields(k.ToolContext)}
live={t[0] for t in cli._LIVE_LIMIT_FIELDS+cli._LIVE_OPTIONAL_INT_FIELDS+cli._LIVE_BOOL_FIELDS+cli._LIVE_STR_FIELDS+cli._LIVE_STR_LIST_FIELDS}
inh=set(m._INHERITED_FIELDS)
print('inherited-not-on-ctx',sorted(inh-ctx)); print('live-not-on-cfg',sorted(live-cfg)); print('ctx&cfg-not-inherited',sorted((ctx&cfg)-inh))"
```
Every setting a merged bead added shows up in this diff. Each one becomes one row in the `KNOBS` table (Part 1).

---

## Part 1: One per-turn settings projection (`js/knobs.py`)

> Done in js-1g1.31 as `js/turn_settings.py`: `TurnSetting` rows in `TURN_SETTINGS`,
> bases `ConfigSettings` and `ContextSettings`. The code is the reference now; the
> text below is the plan as written.

### Current structure (at `31900da`)

| Copy | Location | What it holds |
|---|---|---|
| Registry | js/settings.py:90–390 (`REGISTRY`) plus js/jsrc (one `set` line per setting) | key, type, doc, env var. This stays as the source of truth. |
| Config fields | js/config.py:139–200 | 4 required int fields (`max_tool_iterations`, `max_bash_output_bytes`, `max_tool_result_bytes`, `fetch_timeout_s`:151), 22 `_jsrc_field(...)` fields, `shell_env_allow`, `*_dir` |
| Loader | js/config.py:499–545 (`_int_knob` ×24, `bool(knob(...))` ×7, `kernel_verbosity` choice, `shell_env_allow` validation, `max(1, …)` clamp) plus ~28 matching kwargs in `Config(...)` at 571–628 | |
| Live overlay | js/cli.py:490–534 (`_LIVE_LIMIT_FIELDS` 20 rows, `_LIVE_STR_FIELDS`, `_LIVE_STR_LIST_FIELDS`, `_LIVE_OPTIONAL_INT_FIELDS`, `_LIVE_BOOL_FIELDS`), helpers 537–586, loops in `_cfg_for_live_state` 814–842 | Re-derives the same values from the live store with the active cfg as fallback. It **does not** apply the `max(1, …)` clamp to `max_parallel_tools`, so it has already drifted from the loader. |
| Turn copy | js/runtime.py:1362–1388 | 19 `active_context.X = getattr(cfg, "X", active_context.X)` lines and 5 `_settings.knob(live_settings, ...)` lines (user_agent, shell_program, jail_bind, terminal_cols/rows). Two sourcing rules exist: some fields come from cfg, some from the store. |
| ToolContext fields | js/toolkit/core.py:360–391 | 26 `_knob(...)` fields |
| Subagent inheritance | js/toolkit/meta.py:24–50 (`_INHERITED_FIELDS`, 25 names), used by `_child_context`:107 | |
| Readers | process_net.py:826/847/907, search.py ×5, fetch.md, model_metadata.py:274 | These read `context.fetch_timeout_s` and are the real consumers. |

The interface here is shallow. To add a per-turn setting you have to know six lists and keep them in step, and nothing fails when one is missed except the behaviour.

### Target module and interface

**`js/knobs.py`** holds the settings a turn reads, projected from the settings store. It follows the repo's own "knob" vocabulary (settings.py docstring, `settings.knob()`).

```python
@dataclass(frozen=True)
class Knob:
    attr: str                 # attribute on Config (and ToolContext when on_context)
    key: str                  # registered setting key
    read: Callable[[Any], Any]  # typed store value -> effective value, or INVALID
    on_context: bool = False  # also a ToolContext field; subagents inherit it

KNOBS: tuple[Knob, ...]       # the one place a per-turn setting lands

def project(settings: Mapping | None, fallback: object | None = None) -> dict[str, Any]
    # attr -> effective value for every knob. A missing or invalid value falls back
    # to fallback.attr when a fallback is given, else to the js/jsrc default.
def install(context, cfg) -> None      # copy the on_context knobs cfg -> ToolContext (getattr-tolerant for SimpleNamespace cfgs)
def inherit(parent) -> dict[str, Any]  # on_context knobs of a parent ToolContext
ConfigKnobs   # make_dataclass(frozen=True, kw_only=True, module=__name__), fields from KNOBS
ContextKnobs  # make_dataclass(kw_only=True, module=__name__), fields from the on_context rows
```

Readers are small private functions: `integer`, `at_least_one` (for `max_parallel_tools`), `optional_integer`, `flag`, `choice(...)` (for `kernel_verbosity`), `names` (list of non-empty str to tuple, for `shell_env_allow`), `paths` (for `jail_bind`), `text`, `optional_text`. Each existing coercion moves into exactly one reader.

**Rows (at `31900da`):**
- **on_context (24):** max_read_lines, max_file_bytes, max_read_bytes, max_tool_result_bytes, max_bash_output_bytes, max_bash_output_ceiling, max_tool_result_inline_bytes, fetch_timeout_s, browse_timeout_s, download_timeout_s, max_download_bytes, task_max_depth, subagent_max_workers, shell_env_allow, user_agent, terminal_cols, terminal_rows, kernel_verbosity, kernel_render_max_lines, kernel_wait_seconds, shell_wait_seconds, shell_program, max_parallel_tools, jail_bind.
- **Config only (15):** max_tool_iterations, max_tool_calls_per_message, max_tool_results_per_turn_bytes, inline_code_timeout_s, max_text_attachment_bytes, max_output_tokens (optional), model_context_window (optional), trace, prefer_inherit, lock_subagent_model, allow_inline_code, debug_autolog, debug_autolog_dir, transcript_log, transcript_log_dir.
- **Not rows:** `model`, `reasoning_effort` (provider default plus `_norm_effort`), `vision_enabled`, `debug_log` (derived path), provider/route fields. These are routing concerns and stay where they are.

**Why the attributes stay flat, and why the bases are generated.** The test surface fixes the shape:
- About 27 test helpers build `Config(..., fetch_timeout_s=5, ...)` with keyword arguments, and one does `Config(**{**cfg.__dict__, ...})`.
- Tests construct `ToolContext(cwd=..., fetch_timeout_s=…)` about 60 times and assign `context.max_read_lines = …` about 50 times.

So `cfg.X` and `context.X` must remain flat, constructible, mutable-on-context attributes. Config inherits `ConfigKnobs` and ToolContext inherits `ContextKnobs`. Two bases are needed because a frozen dataclass cannot inherit from a non-frozen one, or the other way round. `kw_only` lets required Config fields (agent_id, …) follow defaulted base fields, and `ToolContext(tmp_path)` still binds `cwd` positionally. Defaults stay `default_factory` so they read js/jsrc lazily; test_package_jsrc swaps `PACKAGE_JSRC` and expects the defaults to follow.

**Alternatives I rejected:**
- (a) A nested `cfg.limits` object. This breaks the ~27 constructors and ~50 assignments above.
- (b) Hand-declared fields plus a drift test. That still leaves three places to edit.
- (c) Putting `attr=`/`on_context=` on `SettingSpec`. This would be literally one place, but it makes settings.py (905 lines, co-changed with cli.py in 30 commits) know the layout of Config and ToolContext and hold reader callables. It is worth revisiting once `knobs.py` has settled.

**Depth and locality.** The interface is one table plus three functions. Behind it sit coercion, fallback rules, the clamp, defaults, field declaration, live re-projection and subagent inheritance, all in one file. A guard test (every `limits.*` key has a row) turns a forgotten projection into a failing test. The seam is `project()`: the loader, the live overlay and tests all cross it with just a settings dict, so no Config is needed to test it.

**Deletion test.** If you delete `knobs.py`, the six copies reappear across config.py, cli.py, runtime.py, core.py and meta.py. The module earns its keep.

### Commits (each keeps `just test` and `just lint` green)

**K1: Add `js/knobs.py` and `tests/test_knobs.py`.** Pure addition. Size S/M: about +170 in `knobs.py`, +130 in tests.
Tests:
- every `Knob.key` resolves through `settings.spec_for`
- every `limits.*` REGISTRY key has a row (the guard)
- `project(seed_defaults())` equals `default_value` per key
- invalid values (bool for int, `"abc"`, None for a required int) fall back
- fallback semantics: missing → `fallback.attr`
- `max_parallel_tools` 0 → 1
- invalid `kernel_verbosity` falls back
- `shell_env_allow` with an empty string falls back

**K2: Generate the bases.** `class Config(knobs.ConfigKnobs)` and `class ToolContext(knobs.ContextKnobs)`. Delete the hand-written knob fields from both, plus `config._jsrc_field` and `core._knob`. The four formerly required Config fields become defaulted. Config gains the store-sourced fields (user_agent, shell_program, jail_bind, terminal_*); they are harmless here because the runtime still reads those from the store. Size S: about −65 / +15. Run the full suite, because this is the structural commit.

**K3: The loader uses the projection.** `config.from_env` calls `Config(..., **knobs.project(js_root_settings))`. Delete ~45 lines of `_int_knob`/`bool(knob)`/choice/validation code, ~28 kwargs, and `_int_knob` and `_numeric_setting`. Size S: about −75 / +3. Tests that pin this: test_memory_config_harness (bad jsrc values fall back), test_package_jsrc, test_settings_jsrc_loader, test_config_runtime_plumbing:74/87, test_config_compaction_layers:27.

**K4: The live overlay uses the projection.** In `_cfg_for_live_state`: `replace(active, **knobs.project(live_settings, fallback=active))`. Delete the five `_LIVE_*` tables and `_live_int_setting`/`_live_optional_int_setting`. Keep `_live_bool_setting`/`_live_optional_str_setting`; the debug-log paths and turn locks at ~2986/3013/3479/3508 still use them. Size S: about −95 / +2. Tests that pin this: test_package_jsrc:204, test_config_runtime_plumbing:126, test_vision_settings_precedence, test_command_table, test_compaction_flight.

**K5: The runtime uses `install()`.** Replace js/runtime.py:1362–1388 with `knobs.install(active_context, cfg)`. `install_context_window_overrides(cfg)`, `active_context.model = model` and the per-turn resets stay. After this, the runtime never reads the store for these settings; the Config is the per-turn projection. Size XS: about −25 / +1. Tests that pin this: test_runtime_offline_integration:877–893, test_config_runtime_plumbing:144, test_package_jsrc:236–258.

**K6: Subagent inheritance uses `inherit()`.** `_child_context` builds `ToolContext(cwd=parent.cwd, model=parent.model, **knobs.inherit(parent))`. Delete `_INHERITED_FIELDS`. Size XS: about −28 / +2. Tests that pin this: test_subagent_isolation:292–333, test_jail `test_a_subagent_is_jailed`.

**K7: Docs.** Add one AGENTS.md sentence next to "Anything settable is a registered setting": *a setting a turn reads is also one `Knob` row in js/knobs.py*. Add a row for `js/knobs.py` to the module table in docs/technical-guide.md (~line 44). Size XS.

Total: about 7 commits, roughly −290 / +330 including ~130 lines of new tests. `fetch_timeout_s` drops from 9 js files to 7 (settings.py, jsrc, knobs.py, process_net.py, search.py, fetch.md, model_metadata.py). It disappears from config.py, cli.py, runtime.py and meta.py, and core.py's fields become generated.

### Tests that move or change
None are edited for Part 1. The `Config(...)` and `ToolContext(...)` keyword constructors, attribute reads, and `replace()` calls all keep working through the generated kw_only bases. One new file is added: `tests/test_knobs.py`.

### Risks
1. **Store-sourced fields change sourcing.** user_agent, shell_program, jail_bind and terminal_* now flow cfg → context, where before they came from the store. A hand-built Config whose `settings` dict disagrees with its fields would behave differently. I found no test that does this: every `settings=` in tests sets compact/tools-alias/ui/runtime-dir keys only.
2. **New live settings.** allow_inline_code, debug_autolog, transcript_log and the `*_dir` fields become live on cfg. This matches AGENTS.md ("settable"), but check the cfg readers at cli.py ~2477 and ~3563.
3. **Editors lose sight of the generated fields.** Jump-to-definition on `cfg.fetch_timeout_s` won't find the generated field. Mitigation: the `KNOBS` table is greppable by attribute name. The lint gate is ruff only, so there is no type-checker impact.
4. **Pickling.** Pass `module=__name__` to `make_dataclass`. I found no pickle or `asdict(cfg)` use today.
5. **Settings added by merged beads.** Use the inventory diff in section 0; each becomes one row in K1.

---

## Part 2: Split `run_turn_async` into its own modules

### Current structure (js/runtime.py at `31900da`)

`run_turn_async` spans 1275–~2160, about 885 lines, with 19 parameters. It has 13 top-level closures, 3 more nested inside them, and 8 `nonlocal` statements.

| Region | Lines | Closures / state |
|---|---|---|
| Resolve overrides, context install | 1305–1404 | Mostly Part 1's copy block |
| Surface persistence | 1334–1348, 1712–1715 | `save_surface` (nonlocal `last_surface`) |
| Events | 1406–1433 | `_emit_event`, `_end_turn` |
| Trace banner | 1435–1470 | inline |
| Streaming display | 1472–1576 | `_emit_reasoning`, `_close_reasoning`, `_muted_transcript_tee`, `_emit_text`, `_commit_streamed_partial`, `_close_text` (nonlocal `answer_display`/`reasoning_display` ×2 each). State: `streamed_text` dict, `streamed_reasoning` list |
| Budget compaction | 1570–1707 | `_budget_context_window`, `_budget_buffer_tokens`, `_active_preserve_from`, `_maybe_compact_request_for_budget` → `_over_budget`, `_history_changed`, `_summarize` (nonlocal `ai_convo` ×2, `changed`, `reclaimed`) |
| Model call and retry | 1716–1924 | Inline `for attempt` loop: preflight budget check, stream, result unpacking, stats, overflow recovery, transport retry via `_backoff`, fatal paths |
| Assistant record | 1926–2030 | inline |
| Dispatch and results | 2032–2105 | inline |
| Error limit, steering, exits | 2106–2160 | inline |

Two defects surface while splitting it:
- `_maybe_compact_request_for_budget` reads `overflow_recovered` (around line 1700) by late binding. That variable is only assigned later, at ~1723, so the closure works only because of call order.
- The overflow "cleared" branch (~1880–1886) repeats `_history_changed` by hand.

### Target modules and interfaces

The modules follow the flat `session_*.py` naming already used in js/.

- **`js/turn_stream.py`**
  - `TurnEvents(event_hooks, telemetry, model, provider_id)` with `.emit(event, **p) -> hooks` and `.end(reason, **extra)`.
  - `StreamSink(telemetry, turn_status, events, *, suppress_output, markdown, reasoning_level)` with:
    - `.text(chunk)` and `.reasoning(chunk)`: the `on_text`/`on_reasoning` adapters passed to `stream_model_async`
    - `.close(reasoning_tokens=None)`
    - `.commit_partial(messages)`: the ^C path
    - `.recorded()`: clears buffers once the assistant record is appended
    - `.new_call()`
  - Depth: display factories, transcript tee muting, reasoning collapse and partial-record semantics sit behind 6 methods.
- **`js/turn_surface.py`**: `SurfaceJournal(session_file, scope, registry)` with `async .restore()` (matches scope, then restores) and `.on_change(state)` (appends only on change; a devnull session never writes).
- **`js/turn_budget.py`**
  - `TurnConvo(system, messages, provider_id, token_state, context)` owns `.ai` (the SDK message list) plus the trace cursor (`sent`, `schemas`), with `.rebuild()`. This replaces both `_history_changed` and the duplicate in the overflow branch.
  - `TurnBudget(cfg, system, messages, convo, token_state, context, telemetry, turn_status, resolve_window)` with `.context_window()`, `async .fit(*, phase, specs, force=False, overflow_round=0) -> bool`, and `.recover_overflow(error, *, overflow_round, specs, max_out) -> "cleared" | ...`.
  - `resolve_window` is injected by the runtime as `lambda: _resolve_context_window(model, provider_id, base_url)`. The lookup stays in runtime's globals, so all 20 test patches of `runtime._resolve_context_window` keep working.
- **`js/turn_call.py`**: `async call_model(request, *, sink, budget, convo, events, telemetry, context, turn_status, mcp_host, call_stats) -> ModelReply | None`. `ModelReply` carries text, pending_calls, finish, reasoning, usage, provider_metadata, incomplete_reason, result and ai_tools. `None` means the retry budget is exhausted. It owns `_backoff` (moved from runtime) and the resilience bead's Retry-After, idle watchdog and max-output recovery once those merge.
- **What stays in runtime.py:** the `run_turn_async` signature, unchanged; module-level `_prepare_turn_context`, `_trace_banner`, `_record_assistant` and `_record_tool_results`; and dispatch (`_dispatch_batch` stays, because it is patched at test_provider_boundary_recovery:283).

Target: `run_turn_async` at ~180–220 lines, with no closures, no nonlocals, and the same signature.

**Seam rules. Violating these makes the 102 stream stubs silently stop applying:**
- Call `model_client.stream_model_async(...)`, `compaction.X` and `stream_transport.X` through the module object. Never `from … import`.
- Keep `result = await _res if inspect.isawaitable(_res) else _res`, because tests use sync stubs.
- turn_* modules must never import runtime; runtime imports them.

### Commits (each green; run `just test-runtime` then `just test`)

**T0: Re-baseline after resilience, compaction and reasoning merge** (section 0 commands). Add characterization tests only where coverage is missing: overflow "cleared" rebuilds the convo and resets the trace cursor; surface restore is skipped on scope mismatch; ^C mid-stream appends a partial record with `incomplete_reason: cancelled`. Grep for existing ones first; test_reasoning_display:89 and test_provider_boundary_recovery already cover some of this. Size S.

**T1: Add `TurnConvo`** in `js/turn_budget.py`. Swap `ai_convo`/`_trace_req` for `convo.ai`/`convo.sent`, and have both compaction paths call `convo.rebuild()`. This removes 2 nonlocals and the duplicated block. Size S: about ±80, plus 40 lines of tests.

**T2: Add `js/turn_surface.py`** and `tests/test_turn_surface.py`. Removes the `last_surface` nonlocal. Size S: about ±60, plus 60 lines of tests.

**T3: Add `js/turn_stream.py`** (`TurnEvents`, `StreamSink`) and `tests/test_turn_stream.py`, which drives the sink with fake `display_factory`/`reasoning_factory`. Removes 4 nonlocals. The cancel path keeps its order: `sink.close()` → `sink.commit_partial(messages)` → `events.end("cancelled")`, with `finally: sink.close_reasoning()`. `runtime.sys.stdout` patches in test_reasoning_display:64 still work because they patch the global `sys`. Size M: about ±220, plus 120 lines of tests.

**T4: Add `TurnBudget`**, with `overflow_round` now an explicit parameter (fixes the late binding), and `tests/test_turn_budget.py`. The tests use a fake `resolve_window` and a patched `compaction.compact_now`, and exercise the clear → summarize-prefix → summarize-turn escalation without running a turn. Removes the last nonlocals (`changed`, `reclaimed` become locals of a method). Size M: about ±250, plus 150 lines of tests.

**T5: Add `js/turn_call.py`** and `tests/test_turn_call.py` (retryable ×2 then raise; overflow → recover → retry; fatal classes; `None` when exhausted). Move `_backoff`. **Test change:** retarget `monkeypatch.setattr(runtime, "_backoff", …)` to `turn_call` at test_runtime_cluster_fixes:124, test_net_channel:264 and test_provider_boundary_recovery:74. The function moved, and AGENTS.md forbids compatibility aliases. Size M/L: about ±300, plus 150 lines of tests. This is the commit most exposed to the resilience bead, so re-read that region right before starting it.

**T6: Flatten the rest.** Module-level `_prepare_turn_context`, `_trace_banner`, `_record_assistant` and `_record_tool_results` in runtime.py. Add the new test files to the `test-runtime` justfile recipe. Size M: about ±200.

### Tests that move or change
- 3 `_backoff` patch targets change (T5), and nothing else. The 20 `_resolve_context_window` patches, 102 `stream_model_async` patches, the 1 `_dispatch_batch` patch and the 2 `runtime.sys` patches stay as they are.
- New unit files: test_turn_stream.py, test_turn_budget.py, test_turn_surface.py, test_turn_call.py.
- Existing runtime-level tests stay as integration coverage. None are deleted.

### Risks
1. **Collision with the three beads.** Do not start before they merge. Re-inventory closures with the grep in section 0; resilience may add a continuation loop, and compaction adds breaker state that belongs on ToolContext or TokenState, not in a closure.
2. **Silent seam loss** from `from model_client import …`. Guard: T3–T5 must keep the full offline suite passing unchanged (stubs are hit or tests fail).
3. **Cancellation ordering and the `finally` cleanup.** Pinned by T0's characterization tests.
4. **Flight data.** `flight_data["ai_messages"]` must capture `convo.ai` before a rebuild, as it does today.
5. **Circular imports.** Prevented by injecting `resolve_window` rather than importing runtime.

### Deletion test
- **Deleted outright:** 13 closures, 3 nested closures, 8 nonlocals, the `streamed_text` dict hack, the duplicated overflow rebuild block, and the late-bound `overflow_recovered` read.
- **The new modules pass it:** deleting `TurnBudget` or `StreamSink` puts their logic back inline with no other way to test it. Each earns its keep.
- **The weakest is `SurfaceJournal`,** at about 40 lines behind 2 methods. If it proves shallow, fold it into `ToolRegistry` later.

### Sequencing across both parts
Part 1 goes first: it is cheaper, and K5 shrinks run_turn's setup block, which makes T6 smaller. Part 1 needs only the settings-adding beads merged. Part 2 needs resilience, compaction and reasoning merged.

---