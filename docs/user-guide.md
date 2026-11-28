# User Guide

`js` is a terminal agent. You give it a prompt, it sends a turn through the
Vercel AI Python SDK (`ai-python`), the model can call local tools, and the
runtime loops until the model returns a final answer or hits a stop condition.
## Install

From the checkout:

```bash
just install
```

It puts `js` and `wiki` on PATH in `~/.local/bin`, fetches js's pinned CLI
binaries into `tools/bin` and the Chromium build `browser_probe` drives, and
then asks for what is missing: each key js uses that is set neither in the
environment nor in `~/.js/.env` (`TYPESAFE_API_KEY`, `TAVILY_API_KEY`,
`EXA_API_KEY`, `SERPER_API_KEY`, `CONTEXT7_API_KEY`; an answer is saved to
`~/.js/.env` at mode 600, Enter skips), and a default model when the configured
one has no provider to run on: pick a saved login and one of its models, or add
a provider through the login flow, and the pick is saved as `MODEL` in
`~/.js/jsrc`. A rerun asks only about what is still missing. Without a terminal
it asks nothing and names what is missing.

The `js` on PATH is a launcher: on every start it brings this checkout's venv in
line with `uv.lock` (`uv sync --no-dev --inexact`, a few milliseconds when nothing
changed) and runs that venv's js, so a dependency added to the project is there
the next time js starts. The launcher finds `uv` on PATH each time it runs; with
no `uv` there it exits 1 (`js launcher: uv not found on PATH`), and when
`uv sync` fails it says so and starts the venv as it stands. It runs the venv's js directly rather than through
`uv run`, which would put the venv first on PATH for every shell command the
model runs. `just uninstall` removes the launcher and the `wiki` symlink.

The install includes the kernel, vision and browser packages. An agent has
kernel/toolbox when its agent.yaml lists them. Playwright does not publish
musllinux wheels, so a musl system goes without the `browser` extra, and a
Termux install carries the base packages only; all other js tools remain
available.
`python -m js` also runs the CLI.

## Basic Configuration
Built-in default model is `cpa/claude-opus-5-5` at effort `medium`; override it
with `JS_MODEL`, `MODEL` in `jsrc`, or `--model` for one run.

Picking a model for every run in a shell:

```bash
export JS_MODEL="cpa/claude-opus-5-5"
js -p "summarize this repo"
```

Local Ollama shortcut:

```bash
js --login ollama
export JS_MODEL="gemma4:e4b"
js -p "describe this image"
```

Generic OpenAI-compatible endpoint (proxy/custom server):

```bash
export JS_PROVIDER="openai"
export JS_BASE_URL="http://127.0.0.1:11434/v1"
export JS_API_KEY="ollama"
js -p "describe this image"
```

`-m` / `--model` overrides the effective configured/env model only for that
run.
Use another model for one run without editing config.

Use another prompt-directory agent:

```bash
js --agent slim -p "inspect the runtime and report risks"
```

## Agent Client Protocol

`js --acp` speaks ACP v1 over stdin and stdout. T3 Code launches it on the
machine hosting the selected environment. In **Settings → Providers → Add
provider → Show more → Local ACP command**, enter the path from
`command -v js` as the executable, `--acp` as an argument, and `js` as the
display name. Open a thread in the project and select the js provider. Leave
the picker on the js default to use the session or configured model, or select
a `provider/model` from the profile's saved model list. The selection survives
a reconnect.

Each argument gets its own row. For example, to use the stock `slim` agent,
enter `--acp`, `--agent` and `slim` in three rows. Add `--model` and your
`provider/model` as two more rows to pin the model for that provider instance.
The launcher needs `uv` on the T3 server's PATH. Use an absolute path for a
custom `--home`. Provider logins, API keys and project settings come from the
same sources as a terminal run.

T3 supplies the working directory and any session MCP servers. Each ACP
session has its own saved js conversation, so T3 can resume it after a
reconnect. Text responses, reasoning and tool activity stream into the thread.
When js requests permission for a tool, T3 applies the thread's permission
mode. A message sent while a turn runs steers it: T3 sends a cancel and then
the prompt, and js keeps the turn running and adds the prompt at its next tool
boundary, so the tool call in progress finishes. A cancel with no prompt
within `ACP_STEER_TIMEOUT` seconds (2) stops the turn; 0 stops it at the cancel.
Each turn runs in a fresh
worker in the session directory. Conversation history and workspace files
carry across prompts. In-memory shell, terminal and kernel handles last for
the turn that opened them. The ACP input supports text, images and text
resources.

## Interactive REPL

```bash
js
```

The REPL loads the selected agent prompt from layered agent directories,
loads the selected session JSONL, and keeps an in-process message list for the
current terminal session.

Provider login:

```bash
js --login                 # curses registry picker (saved/env/known providers)
js --login deepseek        # use DEEPSEEK_API_KEY if present, otherwise prompt
js --login openrouter      # offers OPENROUTER_API_KEY if set
js --login ollama          # local Ollama defaults
js --login llama.cpp       # local llama.cpp defaults
js --login mimo            # Xiaomi MiMo API
js --login mimo-token-plan # Xiaomi MiMo Token Plan
js --logout deepseek       # remove saved login and cached models
```

REPL commands:

```text
/help
/model                         open interactive model picker
/pick-model                    open interactive model picker
/model <model>                 switch model for this session directly
/provider <id>                 switch provider (ollama, llama.cpp, mimo, openai, ...)
/baseurl <url>                 set provider base URL (omit to clear)
/apikey <key>                  set provider API key (omit to clear)
/login <id> [url] [key]        shorthand for /provider + /baseurl + /apikey
/logout                        clear provider/baseurl/apikey for this session
/models [max]                  list available models from the active provider
/set [key [val]]              list settings, show one, or change one
/fset [NAME [value]]          list how js looks: formats, palette, colours, theme
/show [key]                   list all current config values or one key
/save                         write your changed settings into the global jsrc
/load <file>                  run each line of a file as a command (also /source)
/on [event handler]           list or register event hooks
/alias [name [command]]       list, show or define a command alias
/exec <command>               run a shell command; its stdout goes with the next message
/set EFFORT high
/set REASONING_DISPLAY 2     show reasoning and leave it visible (1, the default, folds it)
/set TOOLS_DISPLAY 3         show every tool call and its whole result
/set TOOLS_DISPLAY 1         fold reads and searches, show diffs, running commands and failures
/set COMPACTION_AUTO off
/on turn_start set COMPACTION_AUTO off
/turns
/persona
/tools                         each tool's state (eager/lazy/ban) and the entry that decided it
/session [query]               open the session picker
/skills                        open the skill picker: switch each skill on or off (SKILLS_DISABLED)
/cost                          the session's tokens and cost, in total and by model
/cd [dir]                      print or change the session's working directory
/add <path>[:rw]               under -C, show a path in the jail (read-only, or :rw)
/drop <path>                   under -C, stop showing a path added with /add
/reset
/wipe
exit
quit
:q
```

The `/model` picker lists your saved logins, not every possible SDK provider.
`js --logout <provider>` removes that provider and its cached models from the
picker.

Every command is a row in one table, `COMMANDS` in `js/repl_commands.py`,
assembled from the rows each `js/repl_*_commands.py` module declares; `/help`
and Tab completion read it, so a new row there is a new command everywhere. The
leading `/` is required at the input line and optional in files of commands.

`/load <file>` runs each line of the file through that table, in order, so a
file may hold any command (`model`, `provider`, `alias`, `on`, `set`, ...).
Paths resolve relative to the current project directory; nested `load
other.irc` lines resolve relative to the script that contains them. The first
error stops the file and names the file and line.

`on` stores typed event hooks. The events: `input`, `prompt`, `stream`,
`tool_call`, `tool_result`, `response`, `turn_start`, `turn_end`, `error`,
`cancel`, the `mcp_*` events, and:

- `session_start`: the REPL is up, after the jsrc replay and before the first
  prompt. The REPL fires it on every start, a resume included; `-p` fires it
  before its turns.
- `session_end`: the REPL is closing, after the last turn; `-p` fires it
  before the Continue line.
- `pre_compact`: a compaction is about to summarize the history (`/compact`,
  between turns, or mid-turn). A compaction that finds nothing worth
  summarizing fires neither this nor `post_compact`.
- `post_compact`: the summary replaced the history.

Handler text runs through the same table when the event fires. Handler errors
are captured as event results and debug telemetry instead of aborting the
turn. Nested event dispatch is skipped while a handler is already running. A
`^` prefix on the event is kept with the hook and shown when hooks are listed.

`exec CMD` runs `CMD` under `$SHELL -c` in the session's working directory and
queues its stdout, if any, as one `<js-reminder>` on the next user message.
Typed at the prompt it also shows the output. As a handler, `on EVENT exec
CMD`, the command reads the event on stdin as one JSON object (`{"event":
"tool_call", "id": ..., "name": ..., "arguments": ...}`), and `JS_EVENT`
names it:

```text
on session_start exec git status --short --branch
on post_compact exec cat .js/after-compact.md
on tool_call exec ~/bin/js-guard
```

A `tool_call` handler whose command exits 2 refuses the call: the call never
runs, and the model reads the first line of the command's stderr (else of its
stdout) as the call's result, `ERROR: ...`. Exit 0 lets it run. Exit 3
rewrites it: the command's stdout is the JSON object of arguments the call
runs with instead. The rewrite meets the same schema and argument bans as the
model's own arguments, and one that fails them, or is not a JSON object,
refuses the call. The model's history keeps the arguments it sent; only the
tool sees the rewrite. A later handler reads the call as the one before it
left it. Subagents'
tool calls and a kernel cell's `tools.<name>(...)` calls go through the same
`tool_call` handlers; a subagent's turn fires no other event. Any other status, or running past `EXEC_TIMEOUT`
seconds (30, 0 for no limit; the process group is killed), prints one `exec:`
line and queues nothing. A handler's command holds up the event that ran it
until the shell exits; what it starts in the background (`cmd &`) keeps
running, and its output is read for a moment after the shell exits and then
dropped. `EXEC_OUTPUT_MAX` caps what is kept of each stream. What a
handler queues during a turn reaches the model with the next user message.
`exec` commands are the operator's, not the model's: under `-C` they run on
the host, outside the jail, with js's own environment.

`-p`, a pipe and `--commit` run the `on` lines of the jsrc files (and the
files they `load`) too, on the same events in the same order: `session_start`,
`input`, the turn's events, `pre_compact`/`post_compact`, `session_end`. They
replay nothing else of the jsrc, so only a handler whose command stands alone
runs there: `exec`, `name`, `cost`, `flush` and `refresh-model-catalog`
(`stands_alone` on the command's row in `COMMANDS`). A handler whose command
needs the REPL (`set`, `model`, `fset`, `alias`, `cd`, an alias name, ...) is
reported once on stderr when it registers and never runs. A handler's output
and its errors go to stderr, so stdout keeps only the answer, or the
`--json`/`--json-stream` records. What `exec` would queue for the next user
message is dropped: the run has no next message. Swarm agents run no `on`
handlers.

`/alias name command` defines `/name`. `$*` in the command is replaced by the
alias's arguments; a command without `$*` gets them appended. `/alias -name`
removes one. An alias cannot take the name of a built-in command.

A markdown file `~/.js/commands/NAME.md` is the command `/NAME`, and a
project's `.js/commands/NAME.md` shadows the global one. `/NAME args` sends
the file's text as the user message, with its placeholders filled from the
arguments (split like a shell line; quotes group words): `$1`, `$2` ... one
argument, empty when missing; `$@` or `$ARGUMENTS` all of them; `${N:-text}`
argument N or `text`; `${@:-text}` all of them or `text`; `${@:N}` and
`${@:N:L}` the arguments from the Nth on, or L of them. Text an argument
brings in is not substituted again. YAML frontmatter may give a
`description`, which `/help` shows; without one the first line is shown. Tab
completes the names. A built-in command or an alias of the same name wins.

```text
$ cat ~/.js/commands/review.md
Review $1. Focus on ${2:-correctness}; list findings worst first.
> /review js/cli.py "error paths"
```

`/save` writes what you changed this session into the global jsrc, in place:
each setting you changed gets its line uncommented with your value, a live line
for a setting you set back to stock is commented out, and `on` handlers and
aliases you added are appended. Nothing from the environment, a login or the
defaults goes in, every other line stays as it is, and no backup is made. A
value a line cannot hold as it is (nothing, a line break, space at either
end, a leading `"`) is written as a JSON string literal, `"first\nsecond"`, and a
setting or format given a JSON string literal takes the string it spells,
before any other reading of the value. On
the next start the REPL runs each jsrc file a line at a time, in order, as
`/load` runs a file, its `set` lines (and a setting's short name, e.g. `model X`
is `set model X`) and its commands together, under env and `--extra`. A loaded
file stops at its first error, and so does every loaded file containing it; the
jsrc itself goes on past an error.

`/cd DIR` moves the session: js's working directory and the tools' move to
`DIR`, and the next user message carries one `<js-reminder>` saying the working
directory is now `DIR`. Under `-C`, `/cd` goes only to `DIR` or a bound path;
anywhere else is refused with a pointer to `/add`. `/add PATH` (under `-C`
only) shows `PATH` in the jail from the next tool call on, read-only, or
read-write as `PATH:rw`; the file tools accept it at once. A kernel or terminal
session already running sees it after a restart. `/drop PATH` takes back a path
added with `/add`; the `-C` root cannot be dropped, and neither can the path
the working directory is in. Each of the three tells the model once, and
writes a `workspace:` mark to the session: resuming the session puts the
working directory back, and under the same `-C` it puts the `/add` paths back.
`/cd` and `/drop` wait for a running turn to end.

`/reset` clears the in-process conversation and writes a `session_reset` mark to
the JSONL so future loads ignore older messages in that file.

`/wipe` moves the conversation to a new session file in the same directory,
which can be resumed like any session, then clears the in-process messages.

Reasoning streams in and folds to one line, `N tok reasoning`, when the answer
starts: `REASONING_DISPLAY` 1, the default. `0` hides it, `2` leaves it visible,
and `3` also puts the provider's token count on the open block. Off the screen (`js -p`) nothing can unfold a block, so reasoning never folds there: levels 1 to 3 print its text, and the count line after it when the provider gives one. In the screen, **Ctrl-O** toggles retained reasoning and folded tool rows
without changing the input line. `/save` persists the setting. Hiding or folding
reasoning never removes it from session history or provider replay.

The line above the input is the status bar: `[HH:MM] provider/model context`
on the left, `agent/session cache N% cost` on the right, and while a turn runs a
spinner in the middle with the output-token count, the running tool and its
elapsed seconds, or `compacting`. The cost is the session's spend so far
(`/cost` breaks it down); with no priced call it is a token count. On a narrow
terminal it drops the cache figure first, then shortens the model name, then
drops the provider, the token count, the cost and the agent id; the clock,
spinner and session stay. A session started with `--session NAME` shows its
whole name, cut short with `…` when nothing else is left to drop. Its
colours are `/fset COLOR_STATUS_BACKGROUND #rrggbb` and `/fset COLOR_STATUS_TEXT #rrggbb`, drawn
in truecolor on every terminal, including the Linux console. With more than
one window open the bar shows the window on screen after the clock, `[2]`,
and `[Act: 3,5]` names the windows with output you have not seen.

### Windows

The scrollback is window 1, the main conversation. Each subagent a turn
launches from the REPL opens the next free window, hidden; its launch line in
window 1 says which (`window 2`), or that `WINDOW_MAX` windows (9 by default,
window 1 included) are already open, and that subagent then runs without one,
as under `-p`. A subagent's window shows everything it streams: its launch
and end lines, its answer, reasoning and tool exchanges, each at the levels
window 1 uses. Output in a hidden window marks it on the bar's `[Act: ...]`
list until you show it. A subagent's window that was never on screen closes
when its subagent ends: the parent has the answer and the session has the
rest. `set WINDOW_KEEP_UNSEEN on` keeps them.

`/window` is ircII's: a chain of ops run in order on the current window,
`/window new agent reviewer name rv`, `/window 3 kill`. Any prefix that names
one op works (`/window k`, `/window prev`), and where an op's name starts
every other match, that op (`k` is kill, `h` is hide). A bare number or name
makes that window current. With no ops it lists the windows: `*` the current
one, `+` one with unseen output.

| Op | What it does |
|---|---|
| `list` | List the windows |
| `new` | Open a window under the current one, split; the first line typed there launches the current agent in it as a subagent |
| `agent NAME` | The next line in this window starts a new conversation with agent NAME |
| `kill` | Stop what runs in the window and close it; window 1 stays |
| `kill_others` | Kill every window but this one and window 1 |
| `hide` | Take the window off a split screen; it keeps running |
| `hide_others` | Leave only this window on screen |
| `show N` | Put window N on screen under this one |
| `swap N` | Window N takes this one's place on screen |
| `refnum N`, `N` | Make window N current; a hidden one takes this one's place |
| `goto N` | Make the Nth window on screen, from the top, current |
| `next`, `previous`, `back` | The next or previous window by number, or the one current before |
| `name NAME` | Name the window; `name -` clears it |
| `number N` | Renumber the window, trading numbers with the window that has N |
| `move N` | Move the window N places down the screen, up when negative |
| `grow N`, `shrink N`, `size N` | Ask for N more, fewer, or exactly N rows |
| `balance` | Share the rows evenly again |

**Ctrl-W** then a digit makes that window current, as `/window N` does;
Ctrl-W `n`, `p` and `b` are next, previous and back, Ctrl-W `h` hides the
current window and Ctrl-W `k` kills it. Ctrl-W followed by any other key still
deletes the word before the cursor. Every window keeps running while it is
hidden. On a split screen a line under each window names it.

What you type in a subagent's window goes to that subagent; commands still
run as commands, their output in that window. While the subagent runs, the
line is queued and reaches it as a user message at its next tool boundary,
before its next model call; a line still queued when its task ends runs as
the next turn. Once it has finished, a line runs a new turn on the
subagent's own session with its own agent, model, effort and tools, as
resuming it would. That conversation stays in the subagent's session: the
result its task returned to the parent stands, and the parent is not told.
Ctrl-C in a subagent's window cancels what runs there and drops the lines
queued for it; it leaves window 1's turn alone. The bar in a subagent's
window shows that subagent's model, agent, session and turn.

`NET_DISPLAY` sets how much of the network shows, 0 to 3. Retries print at
every level, 0 included: each one names the failure it follows and its wait,
the wait counts down (on its own line under `-p`, on the status bar in the
screen), and `*** Gave up after N retries: <last failure>` prints when js
stops. js retries a 429, 502, 503, 504 or 529, a network failure (refused,
no connection in `CONNECT_TIMEOUT`, a connection dropped before or during
the reply), and a stream quiet for `STREAM_IDLE_TIMEOUT` after its first
byte; a 500 and any other 4xx fail at once. While a
request waits for its first byte (the prefill) the bar, or a stderr row
under `-p`, counts `waiting for first byte 4m12s`; ^C cancels it, and
`FIRST_BYTE_TIMEOUT` (default 0, no limit) fails it once, unretried. At 1
a failure js does not retry prints too (`*** DNS failure: host`,
`*** 500 ...`, timeouts), and so does what changes the session: each
compaction request (`*** Compacting: model via url`), a reply sent again
with a larger output cap, replayed reasoning the provider refused and js
dropped. Under `-p` those lines print
whatever `NET_DISPLAY` says; a compaction shows there through its own
`*** Compaction` lines, which print at every level in both. At 2 (the default) a response that read more
than 5% (and at least 2000 tokens) less from the prompt cache than the one
before it prints one `*** Prompt cache break` line with both counts and the
seconds since that request; at 3 every other model request, subagents
included, also prints `*** Connecting` and `*** Connected ... Nms`,
and the per-call stream stats line (`ms finish tok tok/s cache`)
prints as well. In the screen that stats
line follows `NET_DISPLAY`; `-p` still shows it with `-d`.

`/set EDITING_MODE vi` makes the input line a vi buffer (the default is
`emacs`, where Enter sends). In vi mode typing starts in insert mode, Enter is a
newline, and Esc then `:` opens the ex line at the bottom:

```text
:w [file]      write the buffer (default: the notes directory); it stays, unsent
:x             send the buffer
:q [note]      quit
:e [file]      edit the buffer (or file) in $VISUAL/$EDITOR; it comes back unsent
:r file        insert a file at the cursor
:n text        append a timestamped note; never sent to the model
:n             open the notes file in $EDITOR
:set k v       any js command, without the /
:nvim          any program on PATH runs on the buffer; it comes back unsent
```

Notes and `:w` saves live in `~/.js/work/notes/`.

A line typed while a turn runs is handled by `STEER_MODE`:

- `now` (default): the line joins the running turn. It reaches the model as a
  user message at the next tool boundary, after the tool results and before
  the next model call, and `*** Steered.` marks the spot. Several lines typed
  before the boundary go in together, in order. If the turn makes no further
  tool call, the lines go in as one message right after it ends.
- `batch`: the lines wait for the turn to end and then go in as ONE message.
- `one`: each line is its own turn, in order.

Tool exchanges follow `TOOLS_DISPLAY`. Each one sits behind a bar down its left
edge, amber while the call runs, green when it finished, red when it failed,
with a mark (`…`, `✓`, `✗`), the tool name in `COLOR_TOOL` on the gray of
`COLOR_PANEL`, and the call's key argument (a path, a search pattern, the
first line of a command, a URL). Exchanges stack with no blank row between.

- `0` shows nothing.
- `1` (the default) shows what matters. Quiet calls are one row, and a run of
  them folds into one row per tool: `read 6 files`, with the file names beside
  it. The quiet tools are `TOOLS_QUIET`; a shell command is quiet when every
  command in it is in `SHELL_QUIET` and it redirects nothing (`ls -la`,
  `git status | head`). Any other shell command shows the last lines of its
  output while it runs, once it has run for a second, and becomes one row with
  its last line when it ends. A `patch` shows its diff, a `send` the message
  sent, any other tool its result, each cut at `TOOLS_PREVIEW_LINES` lines on
  the screen, where Ctrl-O shows the rest.
  A failed call shows its error.
- `2` shows every call on its own with its program and the first
  `TOOLS_PREVIEW_LINES` lines of the result.
- `3` shows every call with its whole result.

On the REPL screen an exchange is redrawn in place as it changes, and Ctrl-O
unfolds a folded row or preview. Elsewhere (`js -p`, a pipe) nothing can unfold
a row, so every call is printed whole when it finishes, at every level but 0.
A saved transcript keeps every call whole too. The colours are /fset `COLOR_*` entries (`/fset COLOR_TOOL #1fb4ff`); `/fset THEME bitchx` picks a palette, from `~/.js/themes/` first and then the built-ins; none is set by default. In
a saved transcript every call is recorded on its own and its header starts
with `>`, so `grep '] > shell'` finds every shell call made at `TOOLS_DISPLAY` 1 or
higher (the `]` keeps `<USER>` lines out). Tool output, tool arguments and
model text are stripped of escape sequences and control bytes before the
terminal sees them.

Assistant Markdown is rendered on a terminal: each finished block (paragraph,
list, fenced code) is highlighted once and stays put; only the block still
being written is redrawn. `/set MARKDOWN off` writes the text as it arrives.
Output that is not a terminal, such as `js -p ... | less`, is plain text.

Ctrl-C cancels the active turn and drops queued and steering lines; `/flush`
drops them without touching the turn. `exit` or Ctrl-D during a turn does what
Ctrl-C does, keeping the partial answer and marking the turn interrupted, and
the screen closes once the turn has ended.

With no turn running, Ctrl-C clears the input line and shows `Press Ctrl-C
again to exit` under it; a second Ctrl-C within two seconds quits. Ctrl-D quits on an empty line and deletes the character under
the cursor inside a line; at the end of a line with text it shows the same
hint, and a second Ctrl-D within two seconds quits.

Resuming a session in the screen shows its last `RESUME_EXCHANGES` exchanges
(3 by default, 0 for none) as a turn draws them, below the startup lines and
the `*** Resumed` and `*** Model` notices.

A paste of more than `PASTE_COLLAPSE_LINES` lines (10) or
`PASTE_COLLAPSE_CHARS` characters (1000) shows in the input line as one
marker, `[paste #N +X lines]` or `[paste #N X chars]`; the line sends the full
text. The scrollback echoes the line with the marker; the
history file keeps the full text. A limit of 0 is no limit on that count.
The terminal must send bracketed paste, which every common terminal does.

### Prompt history and keys

Every line sent at the REPL prompt is appended to one file
shared by every run: `~/.js/state/history.jsonl` (`HISTORY_FILE`), one JSON
object per line with `ts`, `cwd`, `session`, `agent` and `text`. Up walks
every prompt, newest first; `HISTORY_ORDER cwd` walks the prompts typed in the
current directory first, newest first, then the rest. **Ctrl-R** opens an
incremental search over all of them: type to narrow, Ctrl-R again for the next
older match, Enter or Esc to take it into the input line, Ctrl-G to give up.
The newest `HISTORY_SIZE` prompts are loaded. A collapsed paste is
written out whole.

`~/.js/keys` (`KEYBINDS_FILE`) remaps the REPL screen's keys, in jsrc's grammar:

```
bind c-f history_search     # add a key to an action
bind escape r redraw        # a sequence: Esc, then r
unbind c-z                  # take a key off every action
```

A key bound to an action leaves every other action that fires in the same
place. Key names are prompt_toolkit's (`c-r`, `escape`, `enter`, `tab`,
`pageup`, `f5`, `space`, one character). The actions and their default keys:
`submit` Enter, `history_search` Ctrl-R, `ex_open` `:` (vi normal mode),
`ex_run` Enter and `ex_cancel` Esc (in the ex line), `interrupt` Ctrl-C,
`cancel_turn` Esc (in the input line, while a turn runs), `eof`
Ctrl-D, `suspend` Ctrl-Z, `reasoning_toggle` Ctrl-O, `redraw` Ctrl-L,
`scroll_up` PageUp, `scroll_down` PageDown, `complete` Tab, and `window_1` to
`window_9` Ctrl-W then the digit (`bind escape 2 window_2` adds ircII's
Meta-2). A bad line is one
`path:line: error` line in the startup banner and is skipped. Already received text
and reasoning are retained as an interrupted assistant record. A turn with no
recorded progress can be discarded; completed tool work is preserved.

## One-Shot Prompt Mode

```bash
js -p "write a short repo summary"
```

One-shot mode appends the user prompt to a new unique session by default, runs
one turn, prints the final assistant message, persists new messages, and prints
a `*** Continue:` command. A driven agent should keep that default across correction
rounds so it can resume instead of paying to re-read the repository and prior
context on every invocation.

Useful flags:

```bash
js -p "prompt" --session reviews/parser-fix
js -p "apply the review corrections" --session reviews/parser-fix
js -p "throwaway question" --no-save
js -p "prompt" --debug
js -p "prompt" --debug-file /tmp/js-debug.log
js -p "prompt" --reasoning off
js -p "prompt" --max-out 64000
js -p "prompt" --quiet
js -p "prompt" --json
js --migrate-config
```

`--json` writes one final JSON object with the answer, session, provider, model,
effort and usage. `--json-stream` writes JSON Lines as the run proceeds, with
`start`, `reasoning`, `text`, `tool_call`, `tool_result`, `retry`, `error` and
`end` events. The final event contains the same result as `--json`. Other output
goes to stderr. The flags are mutually exclusive. See [Headless JSON](headless-json.md).

`--list-models` and `--list-agents` print text, or JSON with `--json`.
`--list-sessions`, `--list-providers` and `--list-logins` require `--json`.
`--verify --json` reports configuration errors as JSON. A command without the
requested JSON form exits 2 and names the command.

Sampling flags set registered settings for this run: `--temperature`, `--top-p`,
`--top-k`, `--min-p`, `--top-n-sigma`, `--repetition-penalty`, `--presence-penalty`,
`--xtc-probability`, `--xtc-threshold`, `--dry-multiplier`, `--dry-base`,
`--dry-allowed-length`, `--dry-penalty-last-n` and `--dry-sequence-breakers`.
A flag wins over `--extra`, which wins over agent `sampling:`, then jsrc.
For example: `js -p "hello" --temperature 0.7 --top-k 40`.

The Continue command carries the effective route, effort, samplers, home and
prompt/tool options; endpoint keys are omitted. Resuming a chat session keeps
chat mode without needing to repeat `--chat`.

A benchmark always shows complete reasoning, tool calls and results, and raw
markdown. Its JSON keeps its existing metrics and adds `tools`, in call order,
with repeats; unknown tool names have a `?` prefix. `--bench AGENT --json`
prints that object alone; `--json-stream` streams events and ends with it.

`--debug` streams the trace to stdout. `--debug-file` writes the rich trace to a
file and keeps stdout clean. They are mutually exclusive. With `TRACE`
on, a plain one-shot run shows the trace on stderr and keeps stdout answer-only.

`-q` / `--quiet` suppresses the `*** Continue: ...` resume hint that one-shot mode
prints after a saved turn. The session is still written; only the hint is
silenced. `-n` / `--no-save` is different: it makes the run disposable, leaves
stdout answer-only, and prints `*** Session not saved. Resume unavailable.`
to stderr after a headless prompt or pipe run. Agent drivers should treat that
warning as a signal that the next correction round cannot resume and must
re-read context.

## Profiles And Stock Files

`defaults/` ships jsrc, fset, agents, themes, tool descriptions, skills, help,
snippets and secret-patterns. The home overrides a file at the same relative
path; files you have not copied keep coming from stock. Empty files shadow too.
Names in listings union across layers. Agents and skills replace whole directories by name.
Only jsrc, agents, skills, commands and toolbox use a project `.js/` layer.
`defaults/jsrc`, home `jsrc`, project `jsrc`, project `jsrc.local`, env and
`--extra` layer settings; `defaults/fset` loads before format lines in jsrc.

```bash
js --home ~/profiles/review -p "review this patch"
JS_HOME=~/profiles/chat js --chat
```

`--home DIR` wins over `JS_HOME`, then `~/.js`. The selected directory is resolved
to an absolute path once at startup and created when missing. A file in its
place is an error. The whole profile lives there: config, agents, themes,
snippets, credentials, sessions, logs, state, cache and tmp.

Prompt text can use `{{JS_HOME}}` or `%%JS_HOME%%` for the resolved home; inline
commands, envctx and the shell tool receive `$JS_HOME`. A missing named theme or
snippet is an error naming it and its search directories.

## Working Directory And Config Scoping

These flags bind where `js` runs and which config files it reads. They apply to
every mode (`-p`, REPL, `--commit`, ...):

```bash
js -C /path/to/repo -p "summarize this repo"
js --no-local-config -p "prompt"
js --no-global-config -p "prompt"
```

`-C <dir>` keeps the agent in `<dir>`. js changes into the directory before
doing anything, so the working directory, project config lookup, and tools all
see `<dir>`, and it puts the tools in a jail:

- Every tool that starts a process (`shell`, `kernel` and the toolbox on it,
  `terminal_session`, the wiki converters) runs under bubblewrap. `<dir>` is
  bound read-write at its real path. The system is read-only. `/home`, your
  home, `/run/user`, network filesystems (NFS and the like), and any other
  mount that shows your home are empty. The `PATH` directories under them or
  under the host `/tmp` are bound back read-only, so the toolchains on `PATH`
  run, and so is the interpreter the kernel starts. `/tmp` and `~/.js/tmp`
  are directories private to this js process, shared by its commands and
  removed when it exits. The network stays on. The command's environment is
  `SHELL_ENV_KEEP`, so provider keys are not in it.
- The file tools (`read`, `write`, `patch`, `remove`, `undo`, `fs_search`,
  `fetch file://` and `save=`, `browse` screenshots)
  refuse a path outside `<dir>` and the bound paths with one `ERROR` line. A
  path under `/tmp` or `~/.js/tmp` names the file the jailed commands see there.
- Subagents run in the same jail.
- `exec` commands (typed, or run by an `on` handler) are not tools: they run
  on the host with js's environment.
- The `JAIL_BIND` setting shows more paths: a JSON list of `"path"`
  (read-only) or `"path:rw"` entries. The default binds `~/.gitconfig`,
  `~/.config/git`, `~/.local/share/uv` and `~/.cache/uv:rw`, so git and uv
  work in the jail. Tools installed as symlinks into another tree (Homebrew,
  `uv tool`) need that tree bound: `set JAIL_BIND [..., "/home/linuxbrew"]`.
- The `JAIL_HIDE` setting lists paths that are always empty in the jail,
  whatever DIR and `JAIL_BIND` show, and that the file tools refuse. The
  default is `~/.js`, `~/.ssh`, `~/.gnupg`, `~/.netrc`, `~/.aws` and
  `~/.config/gh`. The `~/.js` entry also hides the selected profile under
  `--home` or `JS_HOME`, so a jail rooted at your home still shows no keys.
  Replacing `JAIL_HIDE` keeps the operator's explicit choice; an empty list
  clears these entries. The selected profile's private `tmp` stays available.

`-C` needs `bwrap` (bubblewrap). Without it, or when a startup self-test of the
jail fails, js prints one line and exits; nothing runs unjailed. A missing or
non-directory target, or `/`, is refused the same way. The jail keeps the
agent's context clean; it is not a defence against a hostile model. For a plain
working directory without a jail, `cd <dir> && js`.

Under `-C` the system prompt's `envctx` line says `confined=<dir>` and adds a
rule line telling the model it is confined.

`--no-local-config` ignores the project config files `.js/jsrc` and
`.js/jsrc.local`.

`--no-global-config` ignores `~/.js/jsrc`. The defaults in
`defaults/jsrc` still apply.

`--migrate-config` runs the one-shot legacy-to-`jsrc` conversion and exits; see
[Configuration And Sessions](configuration-and-sessions.md) for the file-level
details.

## Pipe Mode

If stdin is not a TTY, `js` reads stdin as prompt input:

```bash
git diff | js -p "review this patch"
cat notes.md | js
```

Pipe runs use the same saved-by-default session behavior as `-p`. When `-p` is
also supplied, the final prompt is:

```text
<the -p instruction>

<piped stdin>
```

`-p -` means "read stdin as the prompt/operator context" in the places that
accept it.

## File Attachments

Attach files to a prompt instead of pasting them:

```bash
js -p "what's in this chart" -f chart.png
js -p "summarize" -f notes.md -f appendix.txt
cat img.png | js -p "describe this" -f -
```

`-f`/`--file` is repeatable; `-f -` reads bytes from stdin. In the REPL, attach
with an `@path` token in your line (quote spaces: `@"my file.png"`).

In the REPL, **Ctrl-V** pastes the image on the clipboard: `[image #N]` goes in
the line at the cursor, and the image is sent with the line like an `@path`
image. js reads it with `wl-paste --type image/png` under Wayland and
`xclip -selection clipboard -t image/png -o` under X11, and over ssh inside a
kitty terminal with `kitten clipboard -g -m image/png /dev/stdout`, which asks
the kitty on your desk for its clipboard (kitty may show a permission prompt;
see its `clipboard_control`); `PASTE_IMAGE_COMMAND`
names another command that prints the image, and `PASTE_IMAGE_KEY` moves the
key (`/set PASTE_IMAGE_KEY escape v` for Alt-V). With no clipboard, such as
on the Linux console, the key prints one line and the input line stays as it
was. Placeholders count up for the whole run, so a line recalled from history
still carries its image.

- Text files inline into the prompt, delimited. The files of one message share
  `ATTACHMENT_CONTEXT_MAX` (half by default) of the context left before
  compaction starts; the rest is the turn's. A file cut there is named on stderr.
- Images attach as vision input when the active model supports vision; otherwise
  you get a note that vision is off and the bytes are not sent.
- Other binaries attach as a short descriptor (path, type, size).

The `fetch` tool covers the network side — methods, headers, raw/JSON body,
`file://`, downloads to disk, binary descriptors, and image-for-vision.

## Generate media

`js generate --help` lists the operations and flags. Put the operation after
`generate`. For example, `-n 3` requests three images in one request. Ordinary
chat keeps its own `-n` flag.

```sh
export JS_IMAGE_MODEL=cpa/gpt-image-1.5
js generate --t2i -p 'A squirrel with big nuts'
js generate --i2i --image out.png -p 'Keep the squirrel. Make it winter.' -o winter.png
js generate --t2i -u http://minimax-h3-image@localhost:8888/v1 \
  -a h3-image --enhance-url http://refiner-27b@localhost:8080/v1 -p 'An owl at dusk'
```

The bare image run writes `out.png` in the current directory, replacing a file
already there. Use `-o -` for a single artifact on stdout, and
`-p -` or `--prompt-file FILE` for input. Multiple images get numbered paths.
`--dry-run` prints the request, writer route and destinations without submitting
a job. Repeat `--param-json KEY=JSON` for additional native fields.

`-m provider/model` selects a saved login and its model. `-u` selects the
media server directly. A bare `-u http://localhost:8888/v1` takes its model
from `-m` or the operation's setting. The six settings are `IMAGE_MODEL`,
`VIDEO_MODEL`, `TTS_MODEL`, `STT_MODEL`, `MUSIC_MODEL` and `SFX_MODEL`.
Set them in jsrc, with `/set`, or with a `JS_` environment variable.

`--enhance` runs a tool-free text call before submission. `-a NAME`,
`--enhance-model` and `--enhance-url` each enable it. A CLI writer route wins,
then the agent's model, then the normal js text model. The stock writer
follows the operation and the target model's family; `WRITERS` in
`js/generate_writer.py` is the table:

| Operation | Target | Writer |
| --- | --- | --- |
| image | name containing `h3-image` | `h3-image` |
| image | `gpt-image…` | `gpt-image` |
| image | `grok-…` | `grok-image` |
| image | `gemini-…image…`, `nano-banana…` | `gemini-image` |
| image | `imagen-…` | `imagen-image` |
| video | `grok-…` | `grok-video` |
| video | name containing `hailuo` or `minimax-h3` | `h3-video` |
| speech | `gpt-…tts…` | `gpt-speech` |
| music | any | `elevenlabs-music` |
| sound effects | any | `elevenlabs-sfx` |

Any other target gets the general `media-writer`. Use `-a` to pick another
agent, including a colon-qualified name. Agent instructions can include trusted
prompt files using normal js inline directives. The writer receives the
input pictures themselves, in order, when its model takes images, and their
names alone when it does not. For speech, the input script
stays verbatim and the writer supplies delivery instructions to endpoints that
accept them. Native Grok and ElevenLabs speech take delivery cues in the
script and reject `--enhance` and the writer overrides. For music with
`--lyrics`, the writer's text becomes the song's global style.

### Video jobs and Hugging Face Spaces

```sh
export JS_VIDEO_MODEL=hf.space/multimodalart/minimax-h3
js generate --t2v -p 'A squirrel buries an acorn in fresh snow' --duration 4 --submit-only > shot.id
js generate --resume "$(cat shot.id)" -o shot.mp4

js generate --t2i --space MiniMaxAI/minimax-h3-image -p 'A squirrel at dawn' -o dawn.png
js generate --i2v -m cpa/grok-imagine-video-1.5 --first-frame dawn.png -p 'Slow push in' -o dawn.mp4
```

Normally js prints the accepted ID to stderr, waits, downloads the result and
prints its path. `--submit-only` prints the ID alone on stdout. Resume with the
same endpoint and, for a Space, the same function. For a Space image, include
`--t2i` when resuming and repeat `--count N` for a multi-image job. A resume
only retrieves an existing job. Gradio can expire or consume events, so save
the finished artifact while it is available.

Downloads of image and video artifacts are checked before publication.
A response of the wrong media kind leaves an existing output in place.
Artifact downloads from public endpoints connect directly to
checked public addresses on every hop. An endpoint reached on the local
network can also return artifacts on the local network.

Spaces use the Gradio named-argument HTTP API. js reads `HF_TOKEN` or the
active Hugging Face token file so calls can use the account's ZeroGPU quota.
The official `MiniMaxAI/MiniMax-H3-Turbo-Lora` video Space uses `/output_video`.
Its named overrides include `--param-json steps=6` and `--param-json seed=42`.
Other Spaces default to `/generate`. Select a function with `--api-name` and pass
its named inputs with `--param-json`. The H3 image Space takes an ordered
gallery and the H3 video Space above takes first and last frames. Each Space
defines its own function names, inputs, outputs and retention.

### Media endpoints

- OpenAI Images and sd.cpp accept generations and multipart edits under
  `/v1/images`. A ComfyUI bridge exposing these endpoints can use the same
  commands. Grok edits encode image references in JSON. Gemini image models
  on the CPA login use Google's `generateContent`. For a native Google
  endpoint, use a base ending in `/v1beta`. Imagen text-to-image uses
  `:predict` on deployments that serve it.
- Grok video supports submission and retrieval by request ID. Frame anchors,
  reference images, keyframes, audio and clip extension depend on the model.
- OpenAI-compatible speech uses `/audio/speech`. Native Grok uses `/tts` and
  `/tts/voices`. Voxtral uses multipart `/audio/transcriptions`; `--diarize`
  and `--timestamps` preserve the provider's segments in JSON.
- ElevenLabs music, sound effects and speech use a saved login or a model such
  as `elevenlabs/music_v1` with `ELEVENLABS_API_KEY`. For `--lyrics`, js builds
  one section of the requested duration (30 seconds by default). Use
  `--composition-plan FILE` for multiple sections with their own timing.

```sh
js generate --music -m elevenlabs/music_v1 -p 'Brushed jazz trio' --duration 12 --instrumental -o bed.mp3
js generate --tts -m cpa/grok-tts --voice eve -p 'Winter came early.' -o line.mp3
js generate --stt -m mistral/voxtral-mini-latest --audio-file line.mp3 --diarize -o line.json
```

## Commit Mode

The built-in commit workflow is a shortcut for the prompt-directory commit
agent:

```bash
js --commit
js --commit /path/to/repo
js --commit -p "almost all housekeeping tasks"
git diff --stat | js --commit . -p -
```

Semantics:

- `js --commit` targets the current directory.
- `js --commit <dir>` targets that directory.
- `-p "text"` adds operator context.
- `-p -` reads operator context from stdin.
- `--commit` always uses the built-in `commit` agent and rejects `--agent`.
- `-m`, `--session`, `--no-save`, `--debug-file`, `--reasoning`, and
  `--max-out` still work.

The commit agent prompt tells the model to inspect the target repo, group
changes into logical commits, avoid junk, write missing README/CHANGELOG only
when needed, and not push.

## Wiki Agents

Installed `wiki-*` prompt-directory agents own wiki workflows. Friendly wrapper:

```bash
wiki create ~/wiki "research notes"
wiki ingest ~/wiki --unit ~/wiki/inbox/source.md
wiki flow ~/wiki "ingest inbox"
wiki query ~/wiki "what supports this claim?"
wiki lint ~/wiki
```

Native tools keep conversion, page schema/dedup, and ingest close-out deterministic.

## Prompt-Directory Agents

Use a colon between directory names to select a nested agent. For example,
both of these agents can live under `~/.js/agents/meta/`.

```text
~/.js/agents/meta/
├── 01-prompt.md
├── agent.yaml
└── workflow-creator/
    ├── 01-prompt.md
    └── agent.yaml
```

`js -a meta` loads the parent. `js -a meta:workflow-creator` loads the
child. Each uses the prompt files and `agent.yaml` directly in its own
directory. js recognizes a nested agent by its `agent.yaml` or an agent
file such as `01-prompt.md`. Intermediate directories can group children
at any depth. A symlinked directory is read like a real one: a symlinked
group lists the agents beneath it, and a nested symlink is an agent only
when its target holds agent files. A top-level symlink into a network mount
is still an agent, but the listing does not read through it, so a host that
is asleep does not stall a start.

js looks up each full agent name in stock `defaults/agents/`, home `agents/`,
and project `.js/agents/`. The nearest matching agent supplies all its files.
`js --list-agents` lists the names available across those layers.

Within an agent, `*-prompt.md` and `*-env.md` files concatenate in filename
order. `NN-user.md` and `NN-agent.md` seed exchanges; a user file whose last
line is `%%PASS%%`, `%%LOOP%%` or `%%HALT%%` is a control step
(technical-guide, "Control words"). `agent.yaml` selects
tools, model, effort and sampling.

```yaml
tools:
  - read:eager
  - write:eager
  - fs_search:eager
  - patch:eager
  - task:lazy
```

Each entry is `noun:modifier`: `eager` publishes the tool from the first call,
`lazy` puts it in the `tool_discovery` catalog, `ban` removes it. Nouns can be
globs (`"*:ban"`, `wiki_*:lazy`) and `tag:NAME` pulls in a tag from
`~/.js/tools.yaml`. A tool no entry matches is not available; no
entries means the model gets no tools. `/tools` shows the resolved table.
Details: [tool-system.md](tool-system.md).

A run without `--agent` uses the `DEFAULT_AGENT` setting: `set DEFAULT_AGENT slim` in
`~/.js/jsrc` or a project `.js/jsrc`, or `JS_DEFAULT_AGENT=slim`. The built-in
value is `defaultagent`.

## Shell Expectations

The `shell` tool runs commands with the `SHELL_PROGRAM` setting:

`bash -o pipefail -c` by default. `set SHELL_PROGRAM zsh` runs
`zsh -o pipefail -c`; any other program (such as `sh`) runs with `-c` and no
pipefail. A name is looked up on PATH; a path is used as given.

The Python harness does not itself require `fzf` or `bat`. `fs_search` invokes the
pinned `tools/bin/rg` installed by `just install`, falling back to `rg` on PATH
when that binary is missing and `TOOL_BINARY_PATH_FALLBACK` is on (the default),
and reports a plain ERROR when neither is present. Agents can still call `rg`, `fzf`, or `bat` through `shell` when those
programs are installed and useful.

Use the `cwd` argument to the `shell` tool instead of embedding `cd` in the
command string.

## Sessions And Memory

Saved sessions are filed by the project js started in:

```text
~/.js/sessions/<project>/<session>.jsonl   the record
~/.js/sessions/<project>/<session>.txt     a readable transcript
~/.js/sessions/<project>/<session>/        its subagent runs
```

`<project>` is the project root (the nearest `.git`), else the start
directory, as an absolute path with `/` and `_` replaced by `-`. Use
`--session NAME` to continue a saved session from any directory, and `/name
<text>` to title one. A bare `js --session`, or `/session` in the REPL, opens
the session picker: every session, the one last written to first, with search. See [configuration-and-sessions.md](configuration-and-sessions.md).

The memory layer is append-only JSONL plus control marks:

- `session_reset`: future loads ignore earlier messages in that file.
- `rollback_to:N`: future loads truncate the loaded message list to `N`.
- `compaction:{...}`: future loads rebuild context as the unchanged system
  prompt, one `<compaction-summary>` user message, and a safe recent tail.
- `/wipe`: moves the whole file to a new session in the same directory.

Use `/compact [focus]` in the REPL or `js --compact <session>` offline to append
a compaction mark without rewriting the JSONL file. `/compact -m <model>` makes
that one compaction with the named summarizer instead of `COMPACTION_EXTERNAL_MODEL`.
Automatic cache-aware compaction is controlled by `set COMPACTION_AUTO` and the
other `COMPACTION_*` settings (`/set compaction` lists them) in `~/.js/jsrc` or
project `.js/jsrc`.
