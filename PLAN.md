# PLAN.md — remaining FIXME.md bug kills (handoff to Opus 4.8)

(The previous PLAN.md — the Phase 1–6 config-system roadmap — is preserved at
git commit `9990f9e`; its phases are substantially built. This file is the
active plan now.)

Context: FIXME.md documents an evening of bug hunting (2026-07-06/07). A Fable
session diagnosed everything, fixed the biggest clusters, and left the rest
here. Read FIXME.md first, then this. Everything below is DIAGNOSED — the
root-cause work is done; do not re-litigate it, but DO verify each fix live.

## Ground rules (from CLAUDE.md — non-negotiable)

- `just test` (offline suite) green + `just lint` clean before calling anything done.
- Commit with `js --commit` — it works again now that routing is fixed. Never raw `git commit`.
- NO pushing without the owner asking.
- Never crash on bad input: errors must be one friendly actionable line, not tracebacks.
- If running parallel agents: EVERY file-mutating agent gets its own worktree. No exceptions.
- The owner's saved default model may be `testes/test` → local llama.cpp at
  `http://localhost:8050/v1` (may or may not be running). Don't "fix" that config.

## Already fixed in the working tree (uncommitted, tests+lint green) — do not redo

1. **Custom-provider routing (the big FIXME cluster)** — `js/providers.py`:
   `_saved_login_provider_def()` synthesizes a ProviderDef from the logins
   store; `get_provider()` falls back to it; `known_provider_ids()` includes
   saved logins. Regression: `tests/test_custom_login_routing.py` (10 tests).
   Verified live: `js -p`, `js --model "testes/test" -p`, `JS_PROVIDER=testes` all answer.
2. **Login picker** — `js/login_cli.py`: pageup/pagedown/home/end in both curses
   pickers; Ctrl-C returns None instead of a KeyboardInterrupt traceback;
   openai-completions/openai-responses top-level rows deleted (custom covers them);
   blank API key on local/custom providers stores placeholder `x` instead of silently
   aborting; established providers print "login aborted: no API key given"; a fetch
   404 on a base URL without `/v1` prints a hint.
3. **/set validation** — `js/settings.py` `coerce_value`: `provider.id` must name a
   known provider or saved login; `provider.base_url` must start with http(s)://.
   `apply_env_overrides` warns to stderr instead of silently dropping bad env values
   (this was the `JS_BASE_URL="http//..."` evening-eater).
   `/set <key>` and `/show <key>` now print the knob doc line (accepted values).
   Regression: `tests/test_setcmd.py::test_provider_id_and_base_url_validate_at_set_time`.
4. **TUI work** (predates this session, also uncommitted): `js/tui.py` (new),
   `js/cli.py`, `tests/test_nonblocking_repl.py`. Runs fine (`js --tui` in tmux verified).
5. **OOM hole in the shell tool** — `js/toolkit/process_net.py`:
   `subprocess.run(capture_output=True)` buffered ENTIRE command output before
   the 256 KB cap was applied; a runaway command (dmesg shows a `yes` alive in
   the killed session) took js to 92 GB RSS on Jul 4 21:08 and the OOM killer
   ate it. Replaced with `_run_capped()`: threads drain each pipe to EOF but
   retain at most the cap. Verified: 300 MB of output → 38 MB peak RSS.
   Regression: `test_shell_output_capped_while_streaming`.
6. **Login env-key farming killed** — `js/login_cli.py` `_collect_api_login`:
   an env key is now OFFERED (`Found ENV:X (sk-...); use it? [y/N]`), never
   silently used (owner may keep decoy keys in env); a saved key can be
   overridden at re-login (`Enter API Key [enter = keep saved]`). Tests updated
   to the new contract in `tests/test_login_cli.py`.

Note: `JS_TEMP/JS_TOPK/JS_TOPP` turned out to be REAL aliases (`js/sampling.py`
`_ENV_KEYS`) — they likely applied all along; only `/set` display showed
`<unset>` because sampling reads env directly, not the settings store. That's
item 2 below, not a missing feature.

## Owner rulings (2026-07-06, binding — encode them, do not soften)

1. **"Until I explicitly log in, it doesn't matter what my env says."**
   Provider-native env vars (HF_TOKEN, DEEPSEEK_API_KEY, OPENAI_API_KEY, ...)
   must NOT create routes. FIXME2.md shows run5.sh answering under
   huggingface/opencode-go/mimo with zero logins — js env-sniffed his keys.
2. **"What matters is what `js --list-models` says."** The login/model cache is
   the single source of truth for what is runnable.
3. **Login may OFFER an env key, never auto-use one** (implemented, see above).

## Remaining work, priority order

### 0. Encode rulings 1+2: kill env-discovery, login-gate routing

Files: `js/routing.py`, `js/providers.py`, `js/model_client.py`, callers in
`js/cli.py` / `js/config.py` / `js/runtime.py`.

- Delete `discover_env` from `resolve_model_route` (routing.py:104-107) and
  `providers.discover_env_provider` (providers.py). No compat shim — CLAUDE.md
  says removed means GONE. `config.py:324` passes no flag (gets the default
  True today) — it must stop discovering too.
- Prefix routing gate: in `resolve_model_route`, a `provider/model` prefix may
  route ONLY when (a) a saved login exists for that provider (`prefix_login`),
  or (b) the prefix names the explicitly configured provider. A prefix naming
  a KNOWN provider (builtin/catalog) with NO login and no explicit pin must
  ERROR loud: "provider 'huggingface' is not logged in; run `js --login
  huggingface` (js --list-models shows what's runnable)" — NOT silently ride
  env keys, NOT pass through to the default provider. An UNKNOWN first segment
  still passes through whole (OpenRouter/HF-style slashy model ids on the
  routed provider).
- Env credential fill (`providers.provider_base_url/api_key/model` reading
  `key_env`/`base_env`/`model_env`): only honored when the provider was
  explicitly selected (provider.id / JS_PROVIDER / routed prefix WITH login).
  JS_* namespace vars (JS_API_KEY, JS_BASE_URL...) stay: they are explicit
  js-directed instructions, not farming.
- The implicit "unprefixed id routes through AI Gateway with SDK env vars"
  fallback in model_client: same rule — no explicit provider and no saved
  default → actionable error, not a silent gateway ride.
- Tests to update: `tests/test_agent_model_selection.py` fixtures delete env
  keys precisely because of today's sniffing; add regressions asserting (a)
  env keys alone create NO route, (b) un-logged-in known prefix errors loud,
  (c) logged-in prefix still routes (covered by test_custom_login_routing.py).
- After this lands, make the `▸ run` debug line name the credential source
  (`key=login:testes` / `key=env:JS_API_KEY`) so routing is auditable at a glance.

### 1. Compaction / truncated tool-call corruption (FIXME.md top section) — HARDEST

Symptoms: after auto-compaction in a long session, `response incomplete (max
output tokens)` followed by repeated auto-fixed "invalid tool args" pairs, and
`multi_patch` failing with `Unterminated string ... char 1496/1745` — i.e. the
model's tool-call JSON was cut mid-string and the partial args were kept.

Owner's explicit directives (treat as requirements, from FIXME.md "Input:"):
- Compaction must preserve tool-call boundaries / valid JSON strictly, or force
  a clean assistant TEXT turn right after compaction instead of letting the
  model resume into a complex tool call.
- On truncation (finish reason = max output tokens) with a dangling tool call
  whose args don't parse: DROP/abort that tool call into an explicit
  recoverable state; never emit partial args that trigger auto-fix loops.
- Regression tests: compact a turn with pending/large tool arguments
  (multiline patch strings especially); a stream that truncates mid-tool-args.

Entry points:
- `js/runtime.py` — the loop; incomplete-response handling; the tool-args
  auto-repair path (grep `invalid tool args`, `repair`). Recent related commits:
  `7af9886` (canonicalize repaired tool-call args in resent history),
  `f3d39c8` (incomplete response metadata → telemetry/events/history).
- `js/model_client.py` — where the finish reason / incomplete flag comes off the stream.
- Compaction: marks are APPEND-ONLY (sessions JSONL); `compact.*` knobs in
  `js/settings.py`; `js --compact` path in `js/cli.py`.
- Existing tests: grep tests for `compact` and `incomplete` for current contracts.

Suspect origin: owner suspects the multi-agent slop window
`4b091f3..8cffac3` (58 commits, Jul 3–4, agents sharing one worktree).
`git log 4b091f3..8cffac3 -- js/runtime.py` and read with suspicion.

### 2. `/set` must show EFFECTIVE values, not just the settings store

FIXME evidence: launched with `--model openai-completions/...gguf`, the header
shows that model but `/set` says `model.id = openai-codex/gpt-5.5` (stale
store). Sampling env aliases applied but showed `<unset>`. `provider.*` all
`<none>` while a saved login was actually routing.

Design: the no-arg `/set` display resolves the same route the next turn would
use (`js/routing.py::resolve_model_route` + `js/sampling.py` env read) and
annotates sources, e.g.:

    model.id = testes/test                          (saved default)
    provider.base_url = http://localhost:8050/v1    (login: testes)
    sampling.temperature = 1.0                      (env JS_TEMP)

Files: `js/setcmd.py::show_lines` (display), `js/cli.py` (live state has the
flag-derived config), `js/routing.py`. Keep `show_lines(settings, key)` pure
for script processing; add the effective annotations at the REPL call site.

### 3. `model.max_output_tokens` / `compact.context_window` from server metadata

The local server's `/v1/models` reported `n_ctx_train: 262144`; the owner
expects js to know it (`/set` shows `<none>` for both). `js/model_metadata.py`
already maps models.dev metadata; the login flow already fetches the model list.

Design: when `js --login` fetches models from an OpenAI-compatible endpoint,
persist any reported context/max-token metadata into the model cache next to
the cached ids; `runtime._resolve_max_output` and compaction's context-window
guess consult it before falling back. llama.cpp reports `meta.n_ctx_train`;
vLLM reports `max_model_len`. Best-effort — absent metadata keeps today's behavior.

### 4. Friendly provider errors at the model_client boundary

Raw leaks observed: `TypeError: "Could not resolve authentication method..."`
(openai SDK, missing key) and `ValueError: unknown provider id: 'X'` (ai SDK —
mostly prevented by the routing fix now, but still reachable). Map both in
`js/model_client.py` (the ONE provider boundary) to one-line errors naming the
knob to set: which provider, what's missing, the exact `/set` or `js --login`
command. The SDK raise site is `.venv/.../ai/providers/base.py:420`. Add tests
with a fake SDK raising those.

### 5. Login flow: base-URL prompt for catalog providers without one

FIXME: picking a `llama`-ish provider from the picker asked for an API key but
never a Base URL ("Where do I enter my llama url????"). Commit `7306787`
already forces the prompt for `local=True` providers; models.dev catalog
providers aren't marked local. Design: in `js/login_cli.py`, prompt for Base
URL whenever the resolved default base URL is empty, regardless of
`login_base_url_field`. Prefill with the provider default when present.

### 6. Custom logins with the openai-responses shape

`Login` (js/logins.py) stores only `sdk_provider_id` ("openai"/"anthropic"),
not the SHAPE — so `_saved_login_provider_def` (js/providers.py) maps every
openai custom login to `custom_openai` (chat completions). A custom login
created with the openai-responses shape would get the wrong transport.
Design: add optional `shape_provider_id` to `Login` + logins.toml (absent →
current sdk mapping, so existing stores keep working); `login_cli` stores the
chosen `shape_id`; `_saved_login_provider_def` prefers it for the transport.

### 7. Remaining unbounded subprocess captures (same OOM class as the fixed shell tool)

`js/promptexpand.py:190` — inline `!{sh ...}` / fenced code directives run at
PROMPT LOAD with `capture_output=True` and no byte cap: a misbehaving directive
command can OOM a bare `js` launch (matches owner's "launched js by itself and
got an OOM"). `js/runtime.py:732` (compact pre_hook) same pattern, 30s timeout.
Reuse `process_net._run_capped` (move it somewhere shared if that's cleaner
than importing toolkit from promptexpand). Also `fetch` downloads to memory —
streaming-to-disk is a known deferred spec (owner's fetch-universal-puller
idea); at minimum cap the in-memory read.

Context on the OOM history: journal since Jun 25 shows exactly one js kill
(Jul 4 21:08, 92 GB anon-rss, a `yes` process alive in the same session) plus
one `cicc` (NVIDIA compiler, not js). Owner reports more OOMs than the journal
holds; the buffering hole is old (f3ddaa1b, Jun 17) but the slop window armed
it: `796fec3` exposed the shell `timeout` knob to the model (longer runaway
buffering) and the fan-out rework (`c5be54f`, `bf9db06`, `835951c`) multiplied
concurrent shell-running subagents.

### 8. Codex login drops manually-added model ids (FIXME2.md)

`js --login openai-codex`: at "add model ids the list missed" the owner typed
`gpt-5.4,gpt-5.3-codex-spark,gpt-5.4-mini`; output said "cached 2 models" and
`--list-models` shows only `gpt-5.5` + `gpt-5.3-codex-spark` — 5.4 and
5.4-mini vanished. Read `_select_models_to_cache` + the codex login path in
`js/login_cli.py` / `js/codex_auth.py`; find where extras get dropped
(dedupe? validation against the fetched list? the curses multiselect return?).
Regression: extras typed at the prompt must all be cached and listed.

### 9. Parallel `-p` runs interleave onto one tty (FIXME2.md)

run5.sh backgrounds eight `js -p` runs; their outputs and errors interleave
mid-line into the owner's shell (FIXME2 lines 14-25 show a `--list-models`
output invaded by another run's ERROR lines). Consider: when stdout is shared
and `-p` runs concurrently there is no fix js can impose, but js could buffer
its final answer and write it atomically (single write()) instead of streaming
fragments, so parallel runs interleave per-answer instead of per-chunk.

### 10. Request trace dumps to stdout on a plain run (FIXME3)

`JS_PROVIDER=localboy JS_MODEL=... js`, typed `hi` → the terminal got the FULL
request trace: `━━ REQUEST (model_client) ━━`, unclipped system prompt, all 23
tool schemas, messages JSON (~900 lines). No `--debug-file`, no visible
`runtime.trace=on`. Suspect: slop-window commit `c5be54f` "move the request
trace to the model boundary so it dumps the unclipped system prompt and full
tool-schema JSON". Contract: trace goes ONLY to `--debug-file` or when
`runtime.trace=on`; default stdout gets the one-line `▸ run` header at most.
Find where the trace writer picks its stream; add a regression test that a
plain REPL turn writes no trace markers to stdout.

### 11. Login model picker preselects nothing → "cached 0 models" (FIXME3)

First localboy login: fetch found the one model, owner hit enter in the
multiselect, result "cached 0 models" — a trap. `_select_models_to_cache`
(js/login_cli.py) passes `preselected=set()`. Preselect ALL fetched models
(enter = keep everything); `n` still empties. Losing 5.4/5.4-mini in item 8
may be this same trap wearing codex clothes — check both.

### 12a. ROOT CAUSE FOUND: commit messages are command-injected via bash backticks — P0

Autopsy of the failed 2026-07-07 commitbot session (session
`20260707T044659522465Z-63724d1bf5490191`, three bash OOM kills at
00:52/00:55/00:57): the model composed `git commit -m "<multiline body>"`
through the shell tool (`bash -c`), and the body contained
`` one `yes` command took js to 92 GB RSS `` — inside double quotes,
backticks are command substitution, so bash EXECUTED `yes`, buffered its
infinite output in the substitution buffer, hit ~80 GB anon-rss, and the OOM
killer sent SIGKILL (the transcript's `exit=-9`). Every long-message attempt
died; every short/backtick-free message committed fine. Any commit message
containing backticks or `$()` executes arbitrary commands — self-injection.

Fix in CODE (commit_helper's stated philosophy — "both belong in code, not in
the model", js/commit_helper.py:5): add a `commit` subcommand to
`js.commit_helper` that takes the message via stdin or a temp file
(`git commit -F <file>`), and point prompts/commit/01-prompt.md at it the same
way staging already goes through `stage`. Prose must never transit bash
quoting. Regression: a commit message containing backticks/`$()`/quotes/
newlines commits verbatim with no side effects.

### 12b. Agent frontmatter `model:` loses to the saved default — pin is inert

`prompts/commit/00-tools.yaml` now pins `model: deepseek/deepseek-v4-flash`,
but `_apply_agent_model` (js/cli.py:1069) skips the agent model whenever
`cfg.explicit_model` is true — which a `/model`-saved default sets. Verified
live: `js --agent commit -p ...` still routed `model=test provider=testes`.
Owner intent: agent frontmatter should beat the SAVED default but lose to an
invocation-explicit `-m`/`JS_MODEL`. Needs `explicit_model` split into
"explicit on this invocation" vs "saved default from an earlier /model pick"
(config.py knows the source layer). Test both precedences.

### 12c. Commit agent can DESTROY uncommitted work (observed 2026-07-07) — treat as P0

During `js --commit` on the local 35B model, the commit agent reverted several
files while splitting commits (js/settings.py, js/setcmd.py, three test files),
produced a mislabeled bundle (the whole TUI inside "Cap shell tool output"),
then ended with "I need to re-apply all the changes" and died — leaving the
working tree MISSING those changes with no stash, no commit, no snapshot. The
work was only recoverable because the driving session still held the edits.

Commitbot must be crash-safe: before it manipulates the working tree it must
snapshot the full diff (e.g. `git stash create` / a temp patch file) so ANY
failure path can restore. Check its tool surface for `undo`-style reverts and
how it stages per-commit; the restore step must be unconditional. Remember the
prompt rule: fixes here are SUBTRACTION in prompts/commit/01-prompt.md, one
knob at a time — but this specific guarantee may belong in the commit_helper
CODE, not the prompt, so a weak model cannot skip it.

### 13. Small ones

- **TUI double error print** (`js/tui.py`): a turn error renders as `▸ error ...`
  then again as `error: ...` after `▸ turn error`. Decide one; kill the dupe.
- **`js --list-models`** with a big catalog dumps hundreds of lines; owner said
  "shoulda grepped". Accept an optional filter arg (`js --list-models test`)
  substring-matching provider/model.
- **401-then-answers flip** (FIXME.md:300-313): same session, `/set provider.id
  openai` + `api_key x`, one message 401s, the next answers. Likely cause:
  `model.id = openai-codex/gpt-5.5` prefix names a SAVED login (codex), and
  `routing.py:91-96` lets a prefix with a saved login override the pinned
  provider — so routing flip-flops depending on which config each path read.
  Reproduce first; the fix likely falls out of item 2's single-source-of-truth work.

## Verification checklist (run all before done)

    just test && just lint
    js -p "say pong"                                   # saved default routes
    js --model "testes/test" -p "say ok"               # prefix routes (needs :8050 up)
    JS_BASE_URL="http//bad" js -p "x" 2>&1 | head -2   # warns, doesn't silently eat
    js --tui                                            # in tmux: renders, one error line per error
    js --login                                          # picker: pgup/pgdn work, Ctrl-C exits clean

Then `js --commit`, inspect CHANGELOG.md + `git log` per CLAUDE.md step 7.
