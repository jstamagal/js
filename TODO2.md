# TODO

## NORTH STAR — js is its own scripting language
- js is its OWN LANGUAGE.
- Type model: STRINGS ONLY, command/word-oriented (ircII / REXX / Tcl lineage).
- In the REPL, anything starting with `/` goes through the LEXER.
- `set <key> <value>` is the first verb. The verbs are ircII scripting commands:
  - `/IF` — conditional.
  - `/FE` — foreach over a word list; `/FEC` — foreach over characters in a string.
  - `/ON` — bind a script action to an internal event; every client event gets a typed name and `/ON <event> ...` hooks it (e.g. `/on tool_result`, `/on agent_message`, `/on user_send_message`).
  - `/ALIAS` — define a new command.
  - `/EXEC` — run an external process.
  - `/EVAL` — re-parse / evaluate a line after expansion.
  - `/ECHO` — write to the screen.
- Config files are SCRIPTS in this language; js is a SCRIPT LOADER, the config is just the boot script.
- The `set` config system and inline-executable-prompts are early bricks of this.
- `/load <script>` loads a script anytime. REPL prefixes commands with `/`; inside script files commands are SLASHLESS. Snippets load; scripts load scripts; agent prompts can embed scripts.
- Weigh every decision by whether it makes the foundation clean / modular / honest.

## LANGUAGE SEMANTICS
- `set` is for SETTINGS / knobs; `/assign` is for VARIABLES.
- Variables have LOCAL and GLOBAL scope.
- Expansion rule: on the SPINE a direct command does NO `$`-expansion; each `/eval` forces exactly ONE expansion pass. Inside a function/alias, expansion happens normally.
- Canonical examples (on the spine):
  - `/assign foo shoe`       -> foo = shoe
  - `/assign bar $foo`       -> bar = literal `$foo` (not expanded)
  - `/echo $bar`             -> prints `$bar` (zero passes)
  - `/eval echo $foo`        -> shoe (one pass)
  - `/eval echo $bar`        -> `$foo` (one pass: $bar -> $foo, stops)
  - `/eval assign bar $foo`  -> bar = shoe (one pass expands $foo -> shoe, THEN assigns)
- Functions = param-bound aliases, e.g. `alias becho (banner: $0, text: $1-) { echo $banner $text }`; `$1-` = arg 1 through end. `/becho % foo` -> `% foo`.
- Extend js itself by script — no Python edits.

## DECISIONS

- Inlining executable code in prompt/agent files is approved.
- Do not treat executable prompt machinery as a danger.

- Agent config / sampling / env.
  - `00-tools.md` should become a real YAML config file; tool selection belongs in YAML config.
  - Inline executable code belongs in the prompting machinery, not the tools/config file.
  - Keep prompt text separate from agent config.
  - Stop converting prompt/frontmatter sampling into env vars; prompt/agent config must not mutate `os.environ`.
  - CLI flags must not set env vars; their values flow through config/runtime state directly. Audit the existing TODO/FIXME about this.
  - Keep shell env vars like `JS_TEMP` only for operator shell env.
  - Agent sampling/model settings live in real config objects passed to the model call.

- Fetch tool: approved.
  - web/API calls
  - `file://` on purpose
  - methods, headers, raw body, JSON body
  - downloads to disk
  - clean binary handling
  - image handling for vision models
  - quick image/file paste shortcut
  - CLI `-f` / `--file`
  - REPL `@file` shortcut with completion

- Remove / write safety.
  - `remove` stays.
  - Default `remove` uses trash up to 512 MiB; over 512 MiB, stop and ask before direct permanent delete.
  - `remove` must delete the symlink itself, not follow it.
  - `patch` stays strict about exact old text / anchors.
  - whole-file overwrite fails if the file changed since read, unless explicitly allowed.
  - `remove` may allow changed files when explicitly allowed.
  - no receipt bureaucracy.

- Subagent model/provider routing: approved fix.
  - Must route through the right provider pipe, not just swap the model name.
  - Prioritize custom providers.
  - Runtime must remember and use the custom provider's SDK/wire shape, base URL, key, and headers.
  - Support llama.cpp, vLLM, Ollama, local proxies, and OpenAI-shaped endpoints.

- Agent/subagent model selection — make it work, and discoverable.
  - A subagent's own frontmatter `model:` must survive even when AGENTS files are present.
  - `lock_subagent_model` must actually remove the `model` arg from the task tool, not just ignore a passed value; make its doc match.
  - Top-level `--agent <name>` must apply that agent's frontmatter `model`.
  - Show the per-subagent `model` override as an example in the built-in agents.

- Per-wire sampling: send only what the active wire shape accepts; default stays "send nothing". On a non-OpenAI wire, drop OpenAI-only knobs (penalties) so a stray value can't error the transport.

- DeepSeek and MiMo audit.
  - Verify DeepSeek behavior follows current best practices.
  - Verify MiMo behavior follows current best practices.
  - Check reasoning/tool-call/history behavior, not just docs.
  - Be careful with cache behavior; keep cache hot when possible.
  - Always show DeepSeek cache hit rate percentage. Audit whether js captures enough usage data to show it; add reporting if missing.

- vLLM / local OpenAI-shaped servers.
  - Figure out whether it is a vLLM issue or a js issue; assume js may be wrong until proven otherwise.
  - vLLM model names will not contain `deepseek`.
  - Audit reasoning params for vLLM.
  - Audit how js handles context window and max output settings.
  - Do not rely only on model-name pattern matching for local server limits.
  - Investigate whether vLLM / llama.cpp / Ollama servers can be queried for limits/model info; if available, surface it.

- Config and docs correctness.
  - Do not rewrite docs around imaginary users.
  - Docs must be correct.
  - Every real config knob is documented and appears in the config/template/example path.
  - If a knob is removed, erase it completely.
  - Do not write tests forbidding removed names from coming back.
  - Do not write docs explaining that removed things were removed.
  - If config.example shows tuned defaults, say so plainly; do not call tuned defaults generic/public.

- Tests should catch real bugs, not trap.
  - Keep tests that catch real breakage.
  - Do not keep tests just because they match current behavior.
  - If a test protects bad behavior, delete or rewrite it.
  - No ceremony tests.
  - Docs tests should not police exact wording.

- Tool visibility cleanup.
  - If a tool is not included for an agent, the agent should not know it exists.
  - Tool descriptions belong inside the tools.
  - Prompt text should not describe tools outside the selected tool list.
  - Do not add prompt text saying what the default agent cannot do.
  - Remove stale prompt claims about tools that are not selected.

- Wiki vault resolution.
  - Fail closed: if no vault is given and none can be inferred, STOP with a clear error. Provide a vault via `cd` or `--vault`; no default-vault knob.
  - Inference is only: walk up for a `PURPOSE.md` sentinel or a `wiki-*` directory name.
  - Audit that current behavior matches the `PURPOSE.md` design.
  - Make `[wiki.aliases]` a REAL config knob; wire config aliases through `resolve_vault`.
  - Take `creative`/`general` out of the hard-coded built-in aliases; ship them in the stock config's `[wiki.aliases]`.
  - Correct docs that promise the old "falls back to creative" behavior.

- Artifact config must actually apply.
  - `runtime.py` doesn't copy `cfg.artifact_dir/url/bin` onto the tool context; copy them.
  - Precedence: config, then `ARTIFACT_*` env, then built-in default.

- CONFIG SYSTEM = the `set` mechanism. The FIRST build task once this review/decision pass is done.
  - One mechanism: `set <key> <value>` tunes one knob.
  - The config file is a newline list of those `set` lines, read at startup. NOT TOML/YAML.
  - The same `set` works LIVE in the js REPL to change any knob mid-session.
  - A "show all" view lists EVERY knob and its current value.
  - Empty states mean different things:
    - `off` = boolean knob explicitly off.
    - `<none>` = no value set.
    - `<unset>` = sampling params (top_k/top_p/temperature/repetition_penalty) NOT sent at all, so the provider/server default wins.
  - Ship the real defaults as `set` lines (provider/model/baseurl/effort/etc.) plus the `creative`/`general` wiki aliases.
  - Every consumed knob becomes a listed `set` key.
  - Filename: "example" is gone; the config becomes a `set`-script (exact name/path TBD).
  - Check whether js's REPL already has a `set`/slash-command system before building.

- Commit bot. Sampling tuning is not the fix.
  - `commit_helper.py` (deterministic survey + `stage`) was built to replace the manual git survey + blind `git add -p`, but it's orphaned — never wired into any prompt.
  - `~/.config/js/agents/commit_53` is a rewritten prompt that works better than the default, but is prompt-only (model does survey/staging).
  - Finish: make commit_53 the commit agent, wire `commit_helper` into it (deterministic survey + `stage`, not `git add -p`), fix the helper's 3 bugs (O1/O8/O9). Retire the old `prompts/commit/01-prompt.md`.

- `js --commit` must work in any directory, even if it isn't a git repo.
  - Never report a git failure as a clean tree (`_git` honors `check`; distinguish "git failed" / "not a repo" / "clean").
  - Show staged AND unstaged per file, always, as separate sections.
  - Untracked file + a hunk spec other than `all` errors clearly instead of staging the whole file.
  - Handle messy git states without crashing: not-a-repo, empty repo, detached HEAD, mid-merge/rebase, mixed staged+unstaged, untracked.
  - Not a repo: auto `git init`, local, no remote. Personal remote = a later `~/.config/js/agents/commit` override.

## DONE
- Tool-args double-encoding: `_canonical_tool_args` short-circuits only when `json.loads(raw)` is a dict; otherwise repairs via `_repair_jsonish`. `js/runtime.py`; test `test_canonical_tool_args_unwraps_double_encoded_object`.
