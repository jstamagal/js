ORIGINAL TODO.md and TODO2.md, verbatim, BEFORE the KING-scrub + deslop + merge.
Saved for your personal review — to confirm nothing important was lost when "KING this / KING that" was rewritten.

================================================================
ORIGINAL TODO.md
================================================================

# TODO

- Fetch tool: approved whole hog.
  - web/API calls
  - `file://` on purpose
  - methods, headers, raw body, JSON body
  - downloads to disk
  - clean binary handling
  - image handling for vision models
  - quick image/file paste shortcut
  - CLI `-f` / `--file`
  - REPL `@file` shortcut with completion
  - easy way to point a vision model at an image

- Remove/write safety: keep it simple.
  - `remove` stays.
  - `remove` must not follow symlinks.
  - `patch` should stay strict about exact old text / anchors.
  - whole-file overwrite should fail if the file changed since read, unless explicitly allowed.
  - `remove` may allow changed files when explicitly allowed, for stuff like runaway logs.
  - no big receipt bureaucracy.

- Subagent model/provider routing: approved fix.
  - Changing a subagent model must not only swap the model name.
  - It must route through the right provider pipe too.
  - Custom providers matter most.
  - A custom provider can have KING's chosen name but still use an OpenAI-shaped wire adapter.
  - Runtime must remember and use the custom provider's SDK/wire shape, base URL, key, and headers.
  - This needs to work well for llama.cpp, vLLM, Ollama, local proxies, and random OpenAI-shaped endpoints.

- DeepSeek and MiMo audit.
  - DeepSeek and MiMo are crucial providers for KING.
  - Verify DeepSeek behavior is correct and follows current best practices.
  - Verify MiMo behavior is correct and follows current best practices.
  - Check reasoning/tool-call/history behavior, not just docs.
  - Be careful with cache behavior because hot cache pricing can be extremely cheap.
  - KING wants to keep cache hot when possible.
  - KING always wants to know DeepSeek cache hit rate percentage.
  - Audit whether js currently captures enough usage data to show DeepSeek cache hit rate.
  - Add visible DeepSeek cache hit rate reporting if missing.

- vLLM / local OpenAI-shaped servers.
  - KING is having major vLLM trouble.
  - Figure out whether it is a vLLM issue or a js issue.
  - Assume js may be wrong until proven otherwise.
  - KING's vLLM model names will not contain `deepseek`.
  - Sampling params are mostly for vLLM / llama.cpp / Ollama and generally work fine there.
  - Reasoning params may be an issue for vLLM; audit before assuming.
  - Tool calls are a known pain area; some issues may be KING config mistakes, some may be js.
  - Audit how js handles context window and max output settings.
  - KING has been accidentally setting max output/context badly and breaking tool calls.
  - Local server context is based on actual VRAM/runtime config, not what the model can theoretically take.
  - Do not rely only on model-name pattern matching for local server limits.
  - Investigate whether vLLM / llama.cpp / Ollama servers can be queried for useful limits/model info.
  - If server info is available, surface it to help choose sane max output/context settings.

- Config and docs correctness.
  - This is KING's personal tool, not a public product.
  - Defaults are KING's defaults.
  - Do not rewrite docs around imaginary users.
  - Docs still need to be correct.
  - Every real config knob should be documented.
  - Every real config knob should appear in the config/template/example path.
  - Hidden config knobs are not wanted.
  - If a knob exists, show it.
  - If a knob is removed, erase it completely.
  - Once something leaves, never refer to it again.
  - Do not leave hidden legacy variables.
  - Do not write tests forbidding removed names from ever coming back.
  - Do not write docs explaining that removed things were removed.
  - No rule shrines for dead things.
  - If config.example.toml shows KING's tuned defaults, say that plainly.
  - Do not call tuned KING defaults generic/public defaults.
  - Hidden/missing knobs are the problem, not opinionated defaults.

- Tests should help KING, not trap KING.
  - Keep tests that catch real breakage KING cares about.
  - Do not keep tests just because they match current behavior.
  - Current behavior can be wrong.
  - If a test protects bad behavior, delete or rewrite the test.
  - Do not make KING fight a dozen ceremony tests to fix his own tool.
  - Docs tests should not police exact wording.
  - Removed names should vanish completely.
  - Tests should make it easier to use the tool and find real bugs, not harder.

- Tool visibility cleanup.
  - If a tool is not included for an agent, the agent should not know it exists.
  - Tool descriptions belong inside the tools.
  - Prompt text should not describe tools outside the selected tool list.
  - Do not add prompt text saying what the default agent cannot do.
  - Remove stale prompt claims about tools that are not selected.

- Wiki vault heuristic.
  - `inbox/` false-positive issue was fixed.
  - Wiki vault detection should use `PURPOSE.md` as intended.
  - Audit only that the current behavior actually matches the `PURPOSE.md` design.

- Agent config / sampling cleanup.
  - Stop converting prompt/frontmatter sampling into environment variables.
  - Prompt or agent config must not mutate `os.environ`.
  - Keep shell env vars like `JS_TEMP` only for actual operator shell env.
  - Agent sampling/model settings should live in real config objects and be passed to the model call.
  - `00-tools.md` is really config, not prose.
  - Consider renaming `00-tools.md` to a real YAML config file later.
  - Keep prompt text separate from agent config.

- Commit bot consistency.
  - KING ran the tests: sampling tuning is not the fix.
  - Temp 0.2 across 20 trials made variability worse.
  - top_p 0.90 was only meh.
  - Stop treating sampling as the answer for commit-bot consistency.
  - `js` supports inline executable code in markdown agent files.
  - Inline code can run Python/C/etc. before the model sees the prompt.
  - stdout from inline code gets inserted into the prompt.
  - Commit bot should use that path for deterministic repo survey/staging context.
  - Do not rely on the agent deciding to run a helper manually.
  - Check whether the existing Python commit helper is wired through prompt inlining.
  - If not, wire it through prompt inlining or replace it with a deterministic inline workflow.

================================================================
ORIGINAL TODO2.md
================================================================

# TODO2

## NORTH STAR — js is its own scripting language
- js is intended to be its OWN LANGUAGE, not just an agent harness.
- Type model: STRINGS ONLY, modeled on ircII scripting with REXX/Tcl string semantics — command/word-oriented, everything is a string (no typed values). Tcl's `set var value` is the ancestor of `set <key> <value>`.
- In the REPL, anything starting with `/` goes through the LEXER — it's a language construct, not a chat line.
- `set <key> <value>` is the first verb (the config primitive; ircII/Tcl `set`). The verbs are ircII scripting commands (ircII = the reference; KING tweaks as needed):
  - `/IF` — conditional.
  - `/FE` — foreach over a word list; `/FEC` — foreach over characters in a string.
  - `/ON` — bind a script action to an internal event. EVERY client event gets a typed name and `/ON <event> ...` hooks it: e.g. `/on tool_result ...`, `/on agent_message ...`, `/on user_send_message ...`. This is what makes js fully reactive/scriptable — hooks/middleware in KING's own language, not just config.
  - `/ALIAS` — define a new command.
  - `/EXEC` — run an external process (pipe its output in).
  - `/EVAL` — re-parse / evaluate a line after expansion.
  - `/ECHO` — write to the screen.
- Config files are NOT config files — they are SCRIPTS in this language. js is fundamentally a SCRIPT LOADER; the "config" is just the boot script.
- The `set` config system and inline-executable-prompts are early bricks of this, not one-off features.
- `/load <script>` loads a script anytime (ircII `/LOAD`). REPL prefixes commands with `/`; INSIDE script files commands are SLASHLESS (bare `set ...`, like the bitchtearc). Snippets are loadable; scripts can load scripts (composable); agent prompts can embed scripts.
- WHY THIS MATTERS / THE ACTUAL GOAL: all the review + de-slopping + modularizing is runway, not the destination. The point is to get js clean and MODULAR enough that KING can build this scripting language on top. Weigh every decision by whether it makes the foundation clean / modular / honest — that's what the language has to stand on.

## LANGUAGE SEMANTICS (ircII dialect — KING's; he finalizes exact syntax)
- `set` is for SETTINGS / knobs. `/assign` is for VARIABLES. They are different. (Common LLM slop: using `set` for variables — WRONG. qwen gets this right; claude-family models keep getting it wrong. Do not repeat it.)
- Variables have LOCAL and GLOBAL scope.
- Expansion rule: on the SPINE (global, not inside a function) a direct command does NO `$`-expansion; each `/eval` forces exactly ONE expansion pass. Inside a function/alias, expansion happens normally (that's the local-vs-spine difference).
- Canonical examples (on the spine):
  - `/assign foo shoe`       -> foo = shoe
  - `/assign bar $foo`       -> bar = literal `$foo` (not expanded)
  - `/echo $bar`             -> prints `$bar` (zero passes)
  - `/eval echo $foo`        -> shoe (one pass)
  - `/eval echo $bar`        -> `$foo` (one pass: $bar -> $foo, stops)
  - `/eval assign bar $foo`  -> bar = shoe (one pass expands $foo -> shoe, THEN assigns)
- Functions = param-bound aliases, e.g. `alias becho (banner: $0, text: $1-) { echo $banner $text }`; `$1-` = arg 1 through end. `/becho % foo` -> `% foo`. (syntax approximate — KING finalizes.)
- GOAL: once de-slopped, KING extends js ITSELF *by script* — new commands / aliases / behavior in the language, no Python edits.

- Inlining executable code in prompt/agent files is approved.
- Inlining is important.
- Executable prompt machinery is not a problem just because it is powerful.
- Code only runs if it is explicitly put into the agent/prompt.
- If someone does not understand that risk, they should not be using that machinery.
- Do not treat executable prompt machinery like a danger for imaginary careless users.
- This is KING's tool; it can assume KING knows how to use sharp machinery.

- Agent tools/config file cleanup.
- `00-tools.md` should become a real YAML config file.
- Tool selection belongs in YAML config, not fake markdown.
- Inline executable code should not live inside the tools/config file.
- Inline executable code belongs in the actual prompting machinery.
- The point of inlining is to make prompts that are machinery.

- CLI flags should not secretly set environment variables.
- Audit TODO/FIXME note about CLI flags mutating env.
- CLI flag values should flow through config/runtime state directly, not by writing to `os.environ`.

- Remove default behavior approved.
  - Non-chonker means up to 512 MiB.
  - Default `remove` should use trash up to that limit.
  - Over that limit should stop and ask KING before direct permanent delete.
  - `remove` must delete the symlink itself, not follow it.

- Wiki vault resolution.
  - Fail closed: if no vault is given and none can be inferred, STOP with a clear error. No silent fallback to `creative`. You `cd` into a vault or pass `--vault`; no default-vault knob.
  - Inference is only: walk up for a `PURPOSE.md` sentinel or a `wiki-*` directory name. Nothing else.
  - Make `[wiki.aliases]` a REAL config knob. It is documented (config.example, configuration-and-sessions, user-guide, wiki.md) but the code ignores it and only honors the hard-coded {creative, general} dict. Wire config aliases through `resolve_vault`. A documented-but-dead knob is a lie and gets fixed.
  - Take `creative`/`general` OUT of the hard-coded built-in aliases; ship them in the stock config's `[wiki.aliases]` instead, so every alias lives in config and none is baked into code.
  - Docs that promise the old "falls back to creative" behavior get corrected to match fail-closed.

- Artifact config must actually apply. (approved)
  - `runtime.py` copies most config onto the live tool context but forgets `artifact_dir`/`artifact_url`/`artifact_bin`, so `[artifact]` config is ignored and tools fall back to env/defaults (`/srv/artifacts`, `http://localhost`, `artifact`).
  - Fix: copy `cfg.artifact_dir/url/bin` onto the context next to the other limits (3 lines).
  - Precedence: config wins, then `ARTIFACT_*` env as a quick override, then built-in default.

- CONFIG SYSTEM = the `set` mechanism (KING's bitchtea model). The FIRST build task once this review/decision pass is done.
  - One mechanism: `set <key> <value>` tunes one knob. Same grammar as KING's bitchtea rc (`set model ...`, `set baseurl ...`, `set timestamps off`, `set banner %`).
  - The config file is just a newline list of those `set` lines, read at startup. NOT TOML/YAML. File and live command speak the same language.
  - The same `set` works LIVE in the js REPL to change any knob mid-session.
  - A "show all" view lists EVERY knob and its current value, bitchtea-style (`% Value of MODEL is ...`). This IS "every knob visible / no hidden knobs" made literal.
  - Empty states are shown honestly and mean different things:
    - `off` = boolean knob explicitly off.
    - `<none>` = no value set (e.g. profile, disabled_skills).
    - `<unset>` = sampling params (top_k/top_p/temperature/repetition_penalty) NOT sent at all, so the provider/server default wins. (This is the D7 "send nothing by default" decision, made visible. <- my connection, confirm.)
  - The show view doubles as the "crystal-clear default": each knob's shown value (or `<none>`/`<unset>`) tells you exactly what js is doing right now.
  - Ship KING's real defaults as `set` lines (provider/model/baseurl/effort/etc.) plus the `creative`/`general` wiki aliases.
  - Sweeps up the config-surface drift items (O11-O17): every consumed knob becomes a listed `set` key.
  - Filename: "example" is gone; the config becomes a `set`-script like bitchtea's rc (exact name/path TBD).
  - PURPOSE: once every knob is a `set` key with an honest shown value, KING goes down the list and twists each to taste -- rather be mad at defaults he set himself than someone else's.
  - OPEN: does js's REPL already have a `set`/slash-command system to extend, or is this built new? Check before building.

- Tool-args double-encoding (O2 / `fix-tool-args-repair-asymmetry`). DONE + tested.
  - `_canonical_tool_args` only preserved raw bytes when `json.loads` succeeded — but a double-encoded string (valid JSON wrapping the real object) passed that check, so history kept the wrapped blob while execution unwrapped it. Local OpenAI-shaped servers (vLLM/llama.cpp/qwen) emit this; the model then sees its prior call as a quoted blob and imitates it.
  - Fix: short-circuit only when `json.loads(raw)` is a dict; otherwise repair via `_repair_jsonish` so history matches execution. `js/runtime.py`; regression test `test_canonical_tool_args_unwraps_double_encoded_object`. 10/10 tool-args tests pass.

- `js --commit` must JUST WORK in any directory, however messy — even if it isn't a git repo. Robustness + honesty are the spec (KING defers git specifics to me). Covers O1/O8/O9 + more:
  - NEVER lie about state. A git failure must never read as "clean tree / nothing to commit" (O9): `_git` honors `check`; the survey distinguishes "git failed" vs "not a repo" vs "clean".
  - NEVER hide state. Show staged AND unstaged per file, always, as separate labeled sections (O1) so the agent sees the full picture.
  - NEVER silently do the wrong thing. Untracked file + any hunk spec other than `all` errors clearly instead of staging the whole file (O8).
  - Handle every messy git state without crashing: not-a-repo, empty repo (no commits yet), detached HEAD, mid-merge/rebase, mixed staged+unstaged, pure untracked chaos.
  - Not a repo: auto `git init`, local, no remote. Personal remote = a later `~/.config/js/agents/commit` override.
