# Harness performance

What js itself costs for the things it does regularly: starting, reading its
setup, running each tool, saving, the work around a model call, a whole turn,
and sitting idle. Measured with `bench/harness` (`just harnessbench`; see
`bench/harness/README.md`) against a fake model in its own process, so the
times are js's. Numbers are from a 4-core cloud VM on 2026-11-26. "Before" is
bf7771a, which already has the swarm work in `docs/swarm-performance.md`.

## Numbers

| what | before | after |
|---|---|---|
| `js -p` with one short reply, cold, CPU | 1.84 s | 1.37 s |
| `import js.cli` | 0.80 s | 0.77 s |
| `js --help` | 0.81 s | 0.06 s |
| settling a run (agent, prompt, tools) | 56 ms | 35 ms |
| one turn of 40 tool rounds, `-p`, CPU | 2.85 s | 2.00 s |
| the same turn drawn in the REPL, CPU | 4.06 s | 2.12 s |
| the same turn drawn in the REPL, wall | 5.3 s | 2.3 s |
| REPL idle at its prompt | 5.0 ms CPU/s | 3.8 ms CPU/s |
| usage anchor on a 600-message history, per round | 8.3 ms | 0.2 ms |
| budget status on a 600-message history, per round | 19.3 ms | 7.2 ms |

One call of a tool through the dispatcher a turn uses (the handler, plus
argument repair, the trace, the result cap, and drawing it for the console and
the transcript log):

| call | handler before | handler after | whole call before | whole call after |
|---|---|---|---|---|
| `read`, 40 lines | 0.10 ms | 0.10 ms | 27.0 ms | 1.9 ms |
| `read`, 3000 lines | 1.2 ms | 1.2 ms | 1230 ms | 58 ms |
| `write` | 0.3 ms | 0.4 ms | 19.4 ms | 3.0 ms |
| `patch` | 3.1 ms | 3.2 ms | 42.3 ms | 9.9 ms |
| `fs_search`, file names | 4.0 ms | 4.5 ms | 36.4 ms | 7.9 ms |
| `fs_search`, content | 4.9 ms | 5.2 ms | 60.8 ms | 12.9 ms |
| `shell`, `true` | 11.2 ms | 1.9 ms | 15.1 ms | 3.0 ms |
| `plan` | 0.04 ms | 0.06 ms | 6.9 ms | 1.8 ms |
| `remove` | 0.08 ms | 0.12 ms | 5.4 ms | 1.5 ms |
| `notebook_edit` | 0.8 ms | 0.8 ms | 7.0 ms | 2.7 ms |
| `skill` | 5.0 ms | 1.7 ms | 41.8 ms | 6.9 ms |
| `fetch`, local server | 0.7 ms | 0.5 ms | 7.5 ms | 1.7 ms |

Saving a session (300 rounds: 0.9 s to 1.0 s in total, 3 to 4 ms a save) and
replaying a 1.4 MB session (7.5 ms) did not move: each save is two appended
records, each synced to disk.

## What repeated

- **A tool result was drawn two or three times.** The console drew it at the
  TOOLS_DISPLAY level, drew it again whole for the unfold key, and the
  transcript log drew it a third time whole, syntax-highlighted, then stripped
  the colour. Now the whole form is drawn only when a screen can unfold it,
  the log's copy is rendered as plain text, and a folded `read` highlights
  only the rows it shows. A block is printed in one call, not one a row.
- **Every captured command polled.** `capped_process` read its pipes
  non-blocking with a 10 ms sleep and waited for exit through `Popen.wait`'s
  backoff loop, so `true` took 11 ms and each running command woke 200 times
  a second. It now sleeps in `poll()` on the pipes and on the process's pidfd.
  This is under `shell`, prompt `!sh` blocks, and everything else that
  captures a command.
- **The REPL redrew on every token.** A third of a streamed turn's CPU was
  prompt_toolkit laying out the screen. `SCREEN_REDRAWS_MAX` (30) caps the
  redraws a second; 0 is the old behaviour.
- **Each start read the whole models.dev catalog twice**, building 13,000
  model objects each time, to answer whether one model takes images and what
  its context window is. It is read once, and the rows js matches against
  are kept in the cache directory until the catalog database changes.
- **Loading `defaults/jsrc` composed a "NAME is now VALUE" line for each of
  its 250 settings**, each one resolving the theme from disk, and nothing read
  them. A config load now asks for no confirmation lines.
- **Each tool call re-checked its tool's schema against the JSON Schema
  metaschema** before checking the arguments. The validator is kept per schema.
- **Each save re-walked the whole history** for unanswered tool calls, and
  **each round hashed the whole history** as JSON to see whether the last
  usage figure still applied. Both now look at the messages that are new;
  the hash is the fallback when the message objects differ.
- **Colours.** `theme()` resolved twelve colours, each resolving the fset file
  and the theme file from disk two or three times; one read now serves all
  twelve, and `paths.find` builds its cache key from strings.
- **Smaller ones.** The `skill` tool re-parsed every skill's YAML front matter
  on each call (now by file size and modification time). The lexer for a file
  name walked every pygments lexer on each result. Telemetry events were
  JSON-encoded with no trace sink or debug log to write them to. The
  transcript log wrote and flushed a line at a time.

## Imports

- `js --help` printed its usage after importing all of js. `js.entry`, where
  the command now starts, prints it from `js.command_line` alone.
- `js.cli` imported every mode it can run. The REPL, `--bench`, `--commit`,
  `--verify`, `--loop`, `--migrate-config` and the session picker are imported
  by the mode that runs them, which takes about 30 ms off every other start.

## Duplicated code

One pass with pylint's duplicate-code check and a comparison of function
bodies across `js/`. What had two or more copies now has one:

- The session picker and the skill picker each built the same prompt_toolkit
  application, key table and search-key handling: `picker.list_application`
  and `picker.search_key`.
- Logins and the model metadata each walked a served `/models` payload for a
  context window with their own key list and integer check:
  `js/served_limits.py`.
- The MCP client and the MCP host each walked a JSON value redacting its
  strings: `js/mcp/redaction.py`.
- Four places built the same stamp from a config before a save
  (`memory.config_stamp`); two resolved a path against the jail the same way
  (`jail.real_path`); two split a comma list outside brackets
  (`sampling.split_entries`); three statted a file for a cache key
  (`paths.file_stamp`).
- `load_parameter_description` read and searched its tool's description file
  once for each parameter of the tool. The file is split once, and again when
  it changes; building the registry went from 20 ms to 10 ms.

## Measured and left alone

- `import js.cli` is 0.77 s and stays there. `import ai` alone is 0.37 s
  (pydantic building the SDK's message and event models), and twenty js
  modules use it at import, the session history among them, so every mode
  that runs a model or reads a session pays it. The other 0.4 s is 190 js
  modules, none over 12 ms, with prompt_toolkit, rich and markdown-it at
  about 70 ms between them. See "Imports" below for what did move.
- The first model call imports the provider SDK (openai: 0.3 s). The REPL now
  starts that import on a thread while it sits at its prompt; `-p` still pays
  it, since there is no idle moment to hide it in.
- The openai SDK re-validates the whole message list on each request, about
  8 ms a request at 80 messages. That is inside the SDK.
- `paths.isolation` and the project chain re-walk the directories above the
  start directory on each call, about four times a tool round. A stat-stamped
  cache would be faster but can go stale within one timestamp tick, so it
  stays as it is.
- The tool registry is built twice at startup, once for the run's agent
  roots and once for the screen's stock declarations, 4 ms the second time.
- Idle: a REPL at its prompt costs under 4 ms of CPU a second.
