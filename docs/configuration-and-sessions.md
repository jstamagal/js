# Configuration And Sessions

Configuration is read by `js.config.from_env()` at run start and stored in a
frozen `Config` dataclass. CLI flags override individual runs where supported.

## Config Files And Precedence

`js` reads config in this order, lowest to highest:

1. `js/jsrc`, shipped in the package: the built-in defaults
2. user `jsrc` (`~/.js/jsrc`)
3. project `.js/jsrc`
4. project `.js/jsrc.local`
5. env vars
6. `--extra` CLI flags (may be repeated)

In one line: `js/jsrc` < `~/.js/jsrc` < project `.js/jsrc` < project
`.js/jsrc.local` < env vars < `--extra` CLI flags.

A `jsrc` file is a config script: each non-comment line is
`set <key> <value>`, using the same dotted keys as the REPL. Comments start with
`#`. `set -<key>` drops what the layers in between set, so the setting takes its
`js/jsrc` value again.

`js/jsrc` holds one line per registered setting and is where every default value
lives: change a number there and js starts with it. A setting it leaves unset is
written `set -<key>`. If `js/jsrc` is missing, leaves out a registered setting,
or holds a line that does not apply, js stops at startup with one line naming
the file.

No other `jsrc` exists until you write one: js does not create
`~/.js/jsrc`. `/save` writes it, holding only the settings whose live
value differs from `js/jsrc`.

`provider.id`, `provider.base_url`, and `provider.api_key` are unset in
`js/jsrc`. When `provider.id` is set, the provider is constructed explicitly with
the given base URL and API key; otherwise `ai-python` routes the model id
natively through AI Gateway or via `provider:model` syntax for direct providers.

## Setting Reference

Every settable key is registered in `js/settings.py` `REGISTRY` with its type
and help text; its default is its line in `js/jsrc`. `/set` with no argument
lists every setting and its current value; `/show <key>` shows one setting with its
help. Empty-state rendering uses `off` for false booleans, `<none>` for no-value
settings, and `<unset>` for settings that explicitly defer to provider defaults. A set
`provider.api_key` is masked as `<set>`.

Tool alias profiles let a model see alternate tool names without changing the
canonical tool handlers. Profiles are evaluated in order; each `match` value is
case-insensitive substring-matched against the active model id and provider id.
`match` may be a single string or a list of strings.
`aliases` maps canonical active tool names to model-facing names, for example
`{"read":"Read"}`. At runtime, a matching profile with no aliases usable for
the active tool registry is skipped, so a later matching profile can apply.
Aliases that collide with an active canonical tool name are also ignored.

## MCP Servers And Per-Agent Policy

`mcp.servers` and `mcp.agents` are JSON-valued `jsrc` settings. A stdio server
uses `command`, optional `args`/`env`, and an optional `enabled` boolean:

```text
set mcp.servers {"Local Files":{"command":"python","args":["/opt/files-mcp.py"],"env":{"FILES_TOKEN":"secret"}}}
```

A streamable-HTTP server uses `url` and optional `headers`:

```text
set mcp.servers {"Project API":{"url":"http://127.0.0.1:8765/mcp","headers":{"Authorization":"Bearer secret"}}}
```

The value is one JSON object, so combine both entries when using both transports.
Server names must normalize uniquely. A server requires exactly one of `command`
or `url`; HTTP URLs must use `http` or `https` and may not contain userinfo.
Disabled servers are omitted before runtime.

`mcp.agents` applies independent server and namespaced-tool policy by agent id.
Allow/deny entries are case-sensitive glob patterns and deny always wins:

```text
set mcp.agents {"autocoder":{"servers":{"allow":["Local *"],"deny":["*Prod*"]},"tools":{"allow":["local_files__*"],"deny":["*__delete*"]}}}
```

An absent `allow` permits everything not denied; an empty `allow` permits
nothing. The resolved policy is also applied defensively during discovery, so a
denied server is never connected. MCP tools reach an agent through the
canonical `tool_discovery` tool; remote names are not `agent.yaml` entries.

MCP connections are lazy. Merely configuring servers starts no subprocess and
opens no HTTP connection. An MCP-scoped `tool_discovery` call initializes the
matching server(s), and loading one catalog id exposes its full schema only for
the current turn. A normal turn-owned host closes every stdio process and HTTP
session on exit. Session-owned hosts may retain connections across turns, but
loaded MCP schemas do not carry into the next turn.

Configured credential values are redacted from remote schemas, text/structured
results, resources, prompts, errors, progress/log notifications, provider tool
results, and append-only session JSONL. Credential sources include environment
values, header values and authorization payloads, URL query values, and stdio
flag values (`--token value` or `--token=value`). Stdio stderr is drained but
never surfaced. Redaction is a last-resort output boundary, not a reason to put
secrets in prompts or server-controlled content.

## Environment Variables

Registry-backed `JS_*` variables overlay all `jsrc` files and use the same
coercion as `set`. Every setting reads its canonical `JS_<DOTTED_UPPER>` name
(`sampling.top_p` <-> `JS_SAMPLING_TOP_P`); the settings below also take a shorter
name, which wins when both are set. Default values are the lines in `js/jsrc`.

| Variable | Key | Meaning |
| --- | --- | --- |
| `JS_MODEL` | `model.id` | Default model id; unprefixed ids route through AI Gateway. |
| `JS_MAX_OUTPUT_TOKENS` | `model.max_output_tokens` | Per-call max_tokens; unset = models.dev metadata when known, else no explicit cap. |
| `JS_REASONING` | `model.reasoning_effort` | Thinking effort: off\|minimal\|low\|medium\|high\|xhigh\|max (`off` disables thinking); any other value is rejected. |
| `JS_PROVIDER` | `provider.id` | Explicit js provider id (e.g. deepseek, openai-codex, ollama). |
| `JS_BASE_URL` | `provider.base_url` | Explicit provider base URL; unset = provider default. |
| `JS_API_KEY` | `provider.api_key` | Explicit provider API key; unset = env/login default. |
| `JS_MAX_TOOL_ITERATIONS` | `limits.max_tool_iterations` | Max tool calls per turn before the loop gives up. |
| `JS_MAX_BASH_OUTPUT_BYTES` | `limits.max_bash_output_bytes` | Hard cap on shell stdout per call. |
| `JS_MAX_TOOL_RESULT_BYTES` | `limits.max_tool_result_bytes` | Hard cap on any tool result string. |
| `JS_FETCH_TIMEOUT` | `limits.fetch_timeout_s` | fetch() per-request timeout in seconds. |
| `JS_INLINE_CODE_TIMEOUT` | `limits.inline_code_timeout_s` | Timeout in seconds for executable inline prompt directives. |
| `JS_DEBUG` | `runtime.debug` | Append per-event records to `state/<agent>/debug.log`. |
| `JS_TRACE` | `runtime.trace` | Show the per-turn run line, per-call stats and tool exchanges as the model runs. |

Official `ai-python` SDK env vars (`AI_GATEWAY_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_BASE_URL`) are read directly by the provider and
do not need to be copied into `jsrc`.

The agent a run uses is the `agent` setting (`JS_AGENT`, default
`defaultagent` in `js/jsrc`). `set agent autocoder` in a `jsrc` file picks it for
every run under that file; `JS_AGENT` beats the `jsrc` files, and `--agent` or
`--commit` beats both. `--last` and `--session-key` resolve the agent the same
way. The agent is read once at startup: `/set agent` in the REPL does not switch
the running session, and `/save` carries it to later runs. `JS_SESSION` is still
read from the environment. CLI code threads the selected agent and session
through `Config` instead of mutating `os.environ`. Artifact mode is threaded
through `ToolContext`.

`limits.task_max_depth` and `limits.subagent_max_workers` have only their
canonical names, `JS_LIMITS_TASK_MAX_DEPTH` and `JS_LIMITS_SUBAGENT_MAX_WORKERS`.

Code-running inline prompt directives are on by default; `JS_ALLOW_INLINE_CODE=0`
disables them (`--im-a-pussy` sets exactly that for one run). See
[inline-directives.md](inline-directives.md).

## `.env` Files

At startup `js` fills **unset** environment names from `.env` files
(`js/dotenv.py`). This is how tool keys that are read straight from the
environment — `TAVILY_API_KEY`, `EXA_API_KEY`, `SERPER_API_KEY`,
`CONTEXT7_API_KEY` — reach a bare `js` on PATH. `just run` already got them
from the justfile's `set dotenv-load`; this makes both entry points behave the
same.

Files consulted, nearest first:

1. `./.env`
2. each parent directory's `.env`, up to the filesystem root
3. `~/.js/.env`

The real process environment always wins, and the nearest file wins over a
farther one: `TAVILY_API_KEY=x js ...` beats every file, and a project `.env`
beats `~/.js/.env`. Nothing is ever overwritten — only unset names are
filled. `-C DIR` is applied first, so the walk starts at `DIR`.

Format is the usual one: `KEY=value` per line, `#` comments, optional
`export ` prefix, optional single or double quotes around the value. Malformed
lines are skipped rather than raising.

Provider credentials are a separate matter: `js` deliberately does **not**
ride ambient provider keys in place of a login (`js/model_client.py`). A
`.env` sets env vars, so it feeds the same settings env vars do — including
`JS_API_KEY` — but it does not bypass the login gate.

## CLI Overrides

Common flags:

```bash
js -m "model/id"
js --agent autocoder
js --session existing-session
js --no-save
js --debug
js --debug-file /tmp/js-debug.log
js --reasoning off
js --max-out 64000
js --extra limits.task_max_depth=3
js --im-a-pussy
js --migrate-config
```

`--extra KEY=VALUE` sets any dotted config key for one run and wins over env and
all `jsrc` files. It may be repeated. Exact registered keys use the same
registry coercion as `set` and env vars, including JSON validation for structured
settings; values store verbatim, with no magic clear/default token (clearing a
setting back to its default is `set -key`). Loose keys and map subkeys use
generic int -> float -> `true`/`false`/`null` -> string coercion. The key splits
on the first `=` only, so values may contain `=`.

In the REPL, `set [key [val]]` uses the same registry: `set` lists settings,
`set key` shows one value, `set key value` changes the live setting, and
`set -key` puts it back to the value the session started with.
`show [key]` lists every current value or only the requested key. Secret values
such as `provider.api_key` render as `<set>` once set.

`set` also accepts a setting's short name: `model` (`model.id`), `provider`
(`provider.id`), `baseurl` (`provider.base_url`), and `apikey`
(`provider.api_key`).

A `jsrc` file is a file of commands; a leading `/` is optional. While config
loads, the settings layer (`setcmd.apply_config_line`) applies `set` lines and
short-name lines such as `model X`, so they sit under env and `--extra` in
precedence and apply in `-p` runs too. It follows `load` lines and applies the
same lines from the loaded files. When the REPL starts it runs every other
line (`on`, `alias`, `load`, any command in the table) through the command
table, in file order; errors name the file and line and do not stop startup.
A relative `load` path in a `jsrc` resolves against that file's directory.
`/load <file>` runs every line of a file through the same table. Registered
event handlers run through it too when an event is emitted. Handler failures
are recorded on the event emission and in debug telemetry rather than raised
through the model loop; recursive event dispatch from inside a handler is
skipped.

`--migrate-config` is a one-shot conversion for a legacy `config.toml`: it
writes equivalent `set ...` lines to `jsrc` and exits. The migration path is
temporary and is removed after 2 releases.

`--im-a-pussy` opts OUT of the code-running inline prompt directives for the
run; they are on by default and compile and run arbitrary code from prompt
files -- see [inline-directives.md](inline-directives.md).

`--reasoning off` is an explicit override. It disables reasoning even when
`JS_REASONING` is set.

`-m` / `--model` overrides the effective configured/env model for the selected
run or session.

`--max-out` overrides the configured max output tokens for that run. If max
output is unset, the runtime asks models.dev for the active model limit and
otherwise leaves the provider cap alone.
`--refresh-model-catalog` forces an immediate refresh of js's local models.dev
mirror, writes the refreshed timestamp under `~/.js/cache/`, and exits unless you
also requested another action such as `--prompt`.

## Agent Directories

Everything js keeps outside a project lives in `~/.js/`; `js/paths.py` is the
one module that names these locations. Project-local `.js/` files stay with the
project.

```text
~/.js/
  jsrc                   # the user layer; /save writes it
  .env
  JS.md                  # always-on operator context, whatever dir js runs in
  JS.local.md
  tools.yaml
  agents/<agent_id>/     # global agent prompts
  skills/
  toolbox/
  logins/
    logins.toml
    models-cache.json
  sessions/<start-dir>/   # the start directory, / and _ as - (~/js -> -home-me-js)
    <session>.jsonl      # the record, append-only
    <session>.txt        # its readable transcript
    <session>/           # its subagent runs
  state/
    <agent_id>/debug.log
    <agent_id>/undo/
    <agent_id>/latest.json   # the agent's latest session, for --last
    <agent_id>/history       # REPL input history
    kernel/<run>/        # kernel.log and rich-output images
    tool-results/        # oversized results, spilled whole
    commit-backups/
  logs/
    <agent_id>/          # debug autolog, compaction flights
    transcript/<agent_id>/
  cache/modelsdotdev/
  work/                  # what the agent must not lose; notes/ holds :n and :w
  tmp/                   # js scratch; entries a day old are removed at start
  plans/                 # the plan tool
  probes/
    browser/             # browser_probe runs
    terminal/            # terminal_snapshot images
```

Running js from the home directory makes `~/.js` the project `.js/` too; its
`jsrc` then loads once, as the global layer.

### Moving in from the old locations

Before `~/.js`, js kept config in the XDG config directory
(`~/.config/js`), state in the XDG data directory (`~/.local/share/js`), and
agent keepers in `~/inbox/agents/js`. On the first start that finds any of
them, js moves them in and prints one line per move on stderr, then writes
`~/.js/state/home-migrated` so it never runs again. `just migrate-home` shows
the same moves without making them; `just migrate-home --apply` makes them,
marker or not.

- Old config entries land at `~/.js/<name>`, except `logins.toml` and
  `models-cache.json`, which go to `~/.js/logins/`.
- Old data entries land at `~/.js/<name>`, except `transcript` (to
  `logs/transcript`), `modelsdotdev` (to `cache/modelsdotdev`), `notes` (to
  `work/notes`) and `commit-backups` (to `state/commit-backups`).
- `~/inbox/agents/js` becomes `~/.js/work`.

Each entry moves by one rename, so a directory lands whole or not at all. A
symlink moves as the link and is never followed. A relative symlink that the
move would point somewhere else is rewritten to the absolute path it reached
before; one that points at something that moved with it is left as it is. When the destination already
exists, a directory is merged entry by entry, an identical file or link drops
the old copy, and anything else is refused with the reason and left in place.
An entry that cannot be read or compared is refused the same way; the rest
still move. The dry run accounts for its own planned moves, so two old entries
that land in the same place show the same merges and refusals as `--apply`.
A refused entry is reported once at startup; the marker is written anyway, so
`just migrate-home` is how to see it again.

In the same step every agent in `~/.js/agents` is converted to `agent.yaml`
(see `just migrate-agents` in [tool-system.md](tool-system.md)): a tools entry
that matches no tool is dropped, and one line per agent names what was
dropped. A symlinked agent is left for the host it lives on.

Every start creates each directory of the layout above that is missing. Across filesystems an entry is copied beside its destination, renamed into
place, and only then removed from the old location.

Sessions of the old per-agent folders (`sessions/<agent>/`) are filed in the
same step: each under the folder of the working directory in its first start
record, or under `~`'s folder when it has none. An old subagent run carries no
start record; it goes under the parent session whose `task` call carried its
first message. File names are kept, so an old name or hash tail still resumes
with `--session`. A filed session without an agent in its start record gets
one naming the old folder, and each gets its `.txt`. The folder's `.history`
and `latest.json` go to `state/<agent>/`. A session a running js holds open is
left where it is.

The per-agent `state/` directory is created when an agent runs. The agent id is
validated (`^[A-Za-z0-9_-]+$`) *before* any directory is created, so a bad id
never leaves stray files.

### Agent prompts

Agent prompts are discovered from repo `prompts/`, global `~/.js/agents/`,
and project `.js/agents/`; project scope wins over global,
which wins over repo.

Two layers are prepended to every main-agent and subagent system prompt,
blank-line separated:

- `JS.md` and `JS.local.md` from `~/.js/`. These are js's own
  always-on operator context and load whatever directory js runs in. They are
  deliberately NOT called `AGENTS.md`: that name is the per-repo convention a
  dozen other tools also read, so a global one would silently apply
  repo-shaped instructions everywhere.
- `AGENTS.md` and `AGENTS.local.md` from the project, exactly the way any other
  directory's project files load. A `~/.js/AGENTS.md` therefore applies
  only when js is run from inside `~/.js`.

The assembled system prompt is run through inline-directive expansion
([inline-directives.md](inline-directives.md)) before it reaches the model.
Agent manifests are an `agent.yaml` beside the prompt files: `tools:`
entries in `noun:modifier` form, and optional `model:`, `reasoning:`,
`sampling:`, `max_tokens:` and `skills:`. Tags used by `tools:` live in
`~/.js/tools.yaml`. See [tool-system.md](tool-system.md).

## Session Resolution

Sessions are filed by the directory js started in:
`~/.js/sessions/<start-dir>/`, where the folder name is the absolute path with
`/` and `_` replaced by `-`. An agent in `~/js/js/toolkit` greps
`~/.js/sessions/-home-me-js-js-toolkit/*.txt`; a prefix glob
(`-home-me-js*`) covers a tree.

Prompt and pipe runs save by default. Without `--session`, a saved run reserves
a new session named `YYYY-MM-DDTHHMM-xxxx` (local time, four hex digits) in the
current directory's folder and prints a resume command after the answer. This
is the normal choice for an agent driver: use the same session for review and
correction rounds so the model does not have to re-read all prior context.

`--session NAME` resumes an existing session or creates that safe relative name
in the current directory's folder when absent. An existing name is looked up in
the current directory's folder first, then in every folder, so a named session
resumes from any directory. A name that more than one other folder holds is
refused with the paths. A generated name also resumes from a unique tail of
four or more characters. Names may use `/` to group work:

```bash
js --session reviews/parser-fix -p "implement the first pass"
js --session reviews/parser-fix -p "apply the review corrections"
js --session 6d65 -p "resume 2026-09-29T0802-6d65"
```

`--session` with no name is kept for the session picker, which is not built
yet; it says so and exits.

A resumed session continues on the model, provider and reasoning level of its
last stamp (see below) unless the run names them with `--model` or
`--reasoning`. `--last` resumes the agent's most recently started session,
wherever it is filed.

Generated session ids can be resumed from the `*** Continue:` hint. Driver
integrations that have a stable caller key can instead derive an opaque name
from agent + resolved working directory + caller key; repeated runs get the same
`derived/<sha256>` session while different agents, directories, or keys remain
isolated.

`--no-save` uses `os.devnull`. In headless prompt and pipe mode it prints
`*** Session not saved. Resume unavailable.` once on stderr after the run while
keeping stdout answer-only. It does not warn in the interactive REPL. This is an
expensive throwaway choice because the next run cannot resume and must re-read
context.

Absolute session paths are accepted only when they are existing `.jsonl` files
under `~/.js/sessions`. Relative traversal is rejected after resolution.

A subagent run is filed in the folder named after its parent session
(`<session>/task-<epoch>-xxxx.jsonl`), so a plain grep of a directory's folder
does not hit it. The children of an unsaved run are not saved either.

## The `.txt` Transcript

Every write to a session's `.jsonl` brings the `.txt` beside it up to date:

```text
agent: defaultagent   dir: /home/me/js   mode: repl
models: deepseek-v4-flash → xiaomi/mimo-v2.6-pro (#0031)
started: 2026-09-29 08:02   last: 2026-09-29 11:40   turns: 41
branched-from: -
tags: -

#0001 08:02 you  APE
#0015 08:31 tool:shell  look at the spill file  $ wc -lc result.txt  → exit 0, 91984B
#0016 08:32 ape  the file has no real newlines, it's escaped JSON
```

Five header lines and a blank line (`head -6 *.txt` summarises a folder), then
one line per message, numbered by its place among the message records of the
`.jsonl`; a multi-line message continues on indented lines. An assistant
message that calls tools is not a line of its own: its text labels each call,
and each tool result is a line with the tool, that label, the first line of the
call and the result's exit code and size. Tool output is only in the `.jsonl`,
at the same message number. A message a rollback took back out of the
conversation is not shown; compaction removes nothing from the `.txt`.

## JSONL Record Shape

The memory file is append-only JSONL. Records have:

```json
{"kind":"session_metadata","version":3,"ts":1781189999.0,"cwd":"/work/repo","caller_key":"review-42","job_id":"slice-01","agent":"defaultagent","model":"m","mode":"-p","command":["js","-p","..."]}
{"kind":"message","ts":1781190000.0,"version":1,"message":{"role":"user","content":"..."}}
{"kind":"message","ts":1781190002.0,"version":1,"message":{"role":"assistant","content":"..."},"stamp":{"model":"m","provider":"p","reasoning":"high"}}
{"kind":"mark","ts":1781190003.0,"version":1,"marker":"session_reset"}
{"kind":"title","ts":1781190004.0,"title":"parser fix"}
```

Every start appends a `session_metadata` control record: working directory,
agent, model, caller key and job id, how it was started (`mode`: `repl`, `-p`,
`pipe`, `subagent`, `commit`) and the command line. A subagent run's record
names its `parent` session file; a branch's names `branched_from`, the parent
session file and the message number it split at. It is not conversation
context and the message loader ignores it. Adjacent hidden liveness sidecars
track open processes without rewriting the append-only conversation file.

Every assistant message record carries a `stamp`: the model, provider and
reasoning level it was written under. Resume uses the last stamp (or the last
start record's model, whichever came later).

`/name <text>` appends a `title` record; `/name` alone prints the title. The
newest title is the session's name in `--list --json`.

`load_replay_messages()` (and `load_messages()`, which reads through it) ignores:

- malformed JSON lines
- unknown versions
- unknown kinds
- messages whose role is not `user`, `assistant`, `tool`, or `system`

The writer uses `fcntl` locks and `fsync` on append.

## Control Marks

`session_reset` clears the loaded message list at that point in the file.

`rollback_to:N` truncates the loaded message list to `N`. The REPL writes this
when a turn is aborted or a runtime exception happens after the user message was
already appended.

`compaction:{...}` marks rebuild loaded context as one `<compaction-summary>` user message plus a safe tail.

`system:{...}` records the system prompt a session was born with. Every launch
of that session sends those bytes, so a resumed request shares its prefix with
the one that built the conversation.

`prompt_seen:<hash>` is appended at every REPL launch, after `session_start`. The
hash covers the agent prompt files before directive expansion, so output such
as a clock does not count as a change. When the hash differs from the previous
launch's mark, the REPL appends one `<js-reminder>` user message saying the
prompt files changed on disk and the session keeps its original prompt. A
session with no earlier `prompt_seen:` mark gets no notice.

The message loader ignores `system:`, `prompt_seen:` and every other mark; they
remain in JSONL as audit notes.

## Wipe And Backups

`/wipe` rotates the active file:

```text
session.jsonl      -> session.jsonl.bak
session.jsonl      -> session.jsonl.bak.1
session.jsonl      -> session.jsonl.bak.2
```

Existing backups are preserved. The in-process REPL message list is cleared.

## Compaction

Compaction is append-only: the JSONL file is never rewritten. `/compact [focus]`
and `/compact up to here` append a compaction mark; `js --compact <session>` does
the same offline. On load, the mark rebuilds in-memory context as:

1. the unchanged system prompt,
2. one user message containing `<compaction-summary>...`,
3. a fixed tail (`tail_tokens`, default `16384`) whose boundary backs up so an
assistant `tool_calls` message is not separated from its tool results.

The summary model is `compact.model`; literal `same` uses the active session
model. `/compact -m <model>` overrides it for that one manual compaction.
Optional focus text and `compact.pre_hook` stdout are supplied as guidance.
Hook failures warn but do not block compaction.
