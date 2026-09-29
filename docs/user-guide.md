# User Guide

`js` is a terminal agent. You give it a prompt, it sends a turn through the
Vercel AI Python SDK (`ai-python`), the model can call local tools, and the
runtime loops until the model returns a final answer or hits a stop condition.
## Install

```bash
pip install -e ".[test,browser]"
```

The `browser` extra installs the Playwright Python package. The browser itself
is a separate download, so `browser_probe` cannot launch until you also run:

```bash
just install-browser
```

`just install` and `just sync` already do this for you. Playwright does not
publish musllinux wheels, so omit that extra on Alpine and other musl systems;
all other js tools remain available.

The package exposes two scripts:

```bash
js
```

`python -m js` also runs the CLI.

## Basic Configuration
Built-in default model is `deepseek/deepseek-v4-flash`; override it with
`JS_MODEL`, `model.id` in `jsrc`, or `--model` for one run.

AI Gateway / default routing:

```bash
export JS_MODEL="deepseek/deepseek-v4-flash"
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
js --agent autocoder -p "inspect the runtime and report risks"
```

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
js --login ollama          # local Ollama defaults
js --login llama.cpp       # local llama.cpp defaults
js --login mimo            # Xiaomi MiMo API
js --login mimo-token-plan # Xiaomi MiMo Token Plan
js --login openai-codex        # browser OAuth on localhost:1455
js --login openai-codex-device # device-code OAuth; prints URL + code
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
/show [key]                   list all current config values or one key
/save                         rewrite the global jsrc from the live state
/load <file>                  run each line of a file as a command (also /source)
/on [event handler]           list or register event hooks
/alias [name [command]]       list, show or define a command alias
/set model.reasoning_effort high
/set ui.reasoning 2            show reasoning and leave it visible (default)
/set ui.tools 3                show every tool call and its whole result
/set compact.auto off
/on turn_start set compact.auto off
/turns
/persona
/tools                         each tool's state (eager/lazy/ban) and the entry that decided it
/session [query]               open the session picker
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

Every command lives in one table in `js/cli.py` (`COMMANDS`); `/help` and Tab
completion read it, so a new entry there is a new command everywhere. The
leading `/` is required at the input line and optional in files of commands.

`/load <file>` runs each line of the file through that table, in order, so a
file may hold any command (`model`, `provider`, `alias`, `on`, `set`, ...).
Paths resolve relative to the current project directory; nested `load
other.irc` lines resolve relative to the script that contains them. The first
error stops the file and names the file and line.

`on` stores typed event hooks such as `turn_start`, `tool_call`, and
`tool_result`. Handler text runs through the same table when the event fires.
Handler errors are captured as event results and debug telemetry instead of
aborting the turn. Nested event dispatch is skipped while a handler is already
running. The `^` prefix is stored for future suppressive hooks; it does not yet
suppress the default runtime action.

`/alias name command` defines `/name`. `$*` in the command is replaced by the
alias's arguments; a command without `$*` gets them appended. `/alias -name`
removes one. An alias cannot take the name of a built-in command.

`/save` rewrites the global jsrc from everything the session holds: settings
that differ from their defaults, `on` handlers, and aliases. On the next start
the settings layer applies the `set` lines (and a setting's short name, e.g.
`model X` is `set model X`) under env and `--extra`; the REPL then runs every
other jsrc line through the command table.

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

`/wipe` rotates the active session file to `.jsonl.bak`, `.jsonl.bak.1`, and so
on, then clears the in-process messages.

Reasoning streams visibly by default. `/set ui.reasoning 0` hides it, `1`
auto-collapses it when the answer starts, `2` leaves it visible, and `3` adds
token counts. In the standard async screen, **Ctrl-O** toggles retained reasoning
without changing the input line. `/save` persists the setting. Hiding or folding
reasoning never removes it from session history or provider replay.

The line above the input is the status bar: `[HH:MM] provider/model context`
on the left, `agent/session cache N%` on the right, and while a turn runs a
spinner in the middle with the output-token count, the running tool and its
elapsed seconds, or `compacting`. On a narrow terminal it drops the cache
figure first, then shortens the model name, then drops the provider, the
token count and the agent id; the clock, spinner and session id stay. Its
colours are `/set ui.status_bg #rrggbb` and `/set ui.status_fg #rrggbb`, drawn
in truecolor on every terminal, including the Linux console.

`ui.net` sets how much of the network shows, 0 to 3. At 1 only failures print
(`*** DNS failure: host`, `*** 429 ...`, timeouts), once, when js stops
retrying; at 2 (the default) each
model request, including subagents and compaction, also prints
`*** Connecting` and `*** Connected ... Nms`, and the bar counts response
bytes until the first token arrives; at 3 each retry, models.dev catalog
refreshes and the per-call stream stats line (`ms finish tok tok/s cache`)
print as well. In the screen that stats
line follows `ui.net`; `-p` and `--blocking` still show it with `-d`.

`/set ui.editing_mode vi` makes the input line a vi buffer (the default is
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

A line typed while a turn runs is handled by `runtime.steer`:

- `now` (default): the line joins the running turn. It reaches the model as a
  user message at the next tool boundary, after the tool results and before
  the next model call, and `*** Steered.` marks the spot. Several lines typed
  before the boundary go in together, in order. If the turn makes no further
  tool call, the lines go in as one message right after it ends.
- `batch`: the lines wait for the turn to end and then go in as ONE message.
- `one`: each line is its own turn, in order.

Tool exchanges follow `ui.tools`. `0` shows nothing; `1` (the default) shows
one line per exchange, `> read: 4054B 292L`, with the exit status when a shell
command exits nonzero; `2` shows the call with its command highlighted, the
first `ui.tools_preview_lines` lines of the result, `...` when there is more,
and a `read: 1024/4054B 24/292L` line saying what was shown out of the whole;
`3` shows the call and the whole result, with the text of a `read` source file
highlighted. Calls that run at the same time, such as parallel `task` calls,
print each exchange whole when it finishes. Each exchange the screen shows has
one `>` line, so `grep '] > shell'` over a saved transcript finds every shell
call made at `ui.tools` 1 or higher (the `]` keeps `<USER>` lines out). Tool
output, tool arguments and model text are stripped of escape sequences and
control bytes before the terminal sees them.

Assistant Markdown is rendered on a terminal: each finished block (paragraph,
list, fenced code) is highlighted once and stays put; only the block still
being written is redrawn. `/set ui.markdown off` writes the text as it arrives.
Output that is not a terminal, such as `js -p ... | less`, is plain text.

Ctrl-C cancels the active turn and drops queued and steering lines; `/flush`
drops them without touching the turn.

### Prompt history and keys

Every line sent at a REPL prompt, in either REPL, is appended to one file
shared by every run: `~/.js/state/history.jsonl` (`history.file`), one JSON
object per line with `ts`, `cwd`, `session`, `agent` and `text`. Up walks the
prompts typed in the current directory first, newest first, then the rest
(`history.cwd_first off` walks them all in time order). **Ctrl-R** opens an
incremental search over all of them: type to narrow, Ctrl-R again for the next
older match, Enter or Esc to take it into the input line, Ctrl-G to give up.
The newest `history.max_entries` prompts are loaded.

`~/.js/keys` (`keys.file`) remaps the async screen's keys, in jsrc's grammar:

```
bind c-f history_search     # add a key to an action
bind escape r redraw        # a sequence: Esc, then r
unbind c-z                  # take a key off every action
```

A key bound to an action leaves every other action that fires in the same
place. Key names are prompt_toolkit's (`c-r`, `escape`, `enter`, `tab`,
`pageup`, `f5`, `space`, one character). The actions and their default keys:
`submit` Enter, `history_search` Ctrl-R, `ex_open` `:` (vi normal mode),
`ex_run` Enter and `ex_cancel` Esc (in the ex line), `interrupt` Ctrl-C, `eof`
Ctrl-D, `suspend` Ctrl-Z, `reasoning_toggle` Ctrl-O, `redraw` Ctrl-L,
`scroll_up` PageUp, `scroll_down` PageDown, `complete` Tab. A bad line is one
`path:line: error` line in the startup banner and is skipped. The `--blocking`
REPL takes `history_search` from the file; its other keys are prompt_toolkit's. Already received text
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
js --migrate-config
```

`--debug` streams the trace to stdout. `--debug-file` writes the rich trace to a
file and keeps stdout clean. They are mutually exclusive.

`-q` / `--quiet` suppresses the `*** Continue: ...` resume hint that one-shot mode
prints after a saved turn. The session is still written; only the hint is
silenced. `-n` / `--no-save` is different: it makes the run disposable, leaves
stdout answer-only, and prints `*** Session not saved. Resume unavailable.`
to stderr after a headless prompt or pipe run. Agent drivers should treat that
warning as a signal that the next correction round cannot resume and must
re-read context.

## Working Directory And Config Scoping

These flags bind where `js` runs and which config files it reads. They apply to
every mode (`-p`, REPL, `--commit`, ...):

```bash
js -C /path/to/repo -p "summarize this repo"
js --ignore-local -p "prompt"
js --ignore-global -p "prompt"
```

`-C <dir>` keeps the agent in `<dir>`. js changes into the directory before
doing anything, so the working directory, project config lookup, and tools all
see `<dir>`, and it puts the tools in a jail:

- Every tool that starts a process (`shell`, `kernel` and the toolbox on it,
  `terminal_session`, the wiki converters) runs under bubblewrap. `<dir>` is
  bound read-write at its real path. The system is read-only. `/home`, your
  home, `/run/user`, network filesystems (NFS and the like), and any other
  mount that shows your home are empty. The `PATH` directories under them are
  bound back read-only, so the toolchains on `PATH` run. `/tmp` and `~/.js/tmp`
  are directories private to this js process, shared by its commands and
  removed when it exits. The network stays on. The command's environment is
  `limits.shell_env_allow`, so provider keys are not in it.
- The file tools (`read`, `write`, `patch`, `remove`, `undo`, `fs_search`,
  `ast_search`, `list_dir`, `fetch file://` and `save=`, `browse` screenshots)
  refuse a path outside `<dir>` and the bound paths with one `ERROR` line. A
  path under `/tmp` or `~/.js/tmp` names the file the jailed commands see there.
- Subagents run in the same jail.
- The `jail.bind` setting shows more paths: a JSON list of `"path"`
  (read-only) or `"path:rw"` entries. The default binds `~/.gitconfig`,
  `~/.config/git`, `~/.local/share/uv` and `~/.cache/uv:rw`, so git and uv
  work in the jail. Tools installed as symlinks into another tree (Homebrew,
  `uv tool`) need that tree bound: `set jail.bind [..., "/home/linuxbrew"]`.

`-C` needs `bwrap` (bubblewrap). Without it, or when a startup self-test of the
jail fails, js prints one line and exits; nothing runs unjailed. A missing or
non-directory target, or `/`, is refused the same way. The jail keeps the
agent's context clean; it is not a defence against a hostile model. For a plain
working directory without a jail, `cd <dir> && js`.

Under `-C` the system prompt's `envctx` line says `confined=<dir>` and adds a
rule line telling the model it is confined.

`--ignore-local` ignores the project config files `.js/jsrc` and
`.js/jsrc.local`.

`--ignore-global` ignores `~/.js/jsrc`. The defaults in the package's
`js/jsrc` still apply.

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
`xclip -selection clipboard -t image/png -o` under X11; `ui.paste_image_command`
names another command that prints the image, and `ui.paste_image_key` moves the
key (`/set ui.paste_image_key escape v` for Alt-V). With no clipboard, such as
on the Linux console, the key prints one line and the input line stays as it
was. Placeholders count up for the whole run, so a line recalled from history
still carries its image.

- Text files inline into the prompt (delimited, up to 64 KiB).
- Images attach as vision input when the active model supports vision; otherwise
  you get a note that vision is off and the bytes are not sent.
- Other binaries attach as a short descriptor (path, type, size).

The `fetch` tool covers the network side — methods, headers, raw/JSON body,
`file://`, downloads to disk, binary descriptors, and image-for-vision.

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

Agents are discovered from repo `prompts/`, global `~/.js/agents/`, and
project `.js/agents/`; project scope wins over global, which wins over repo.
Each agent lives at `<root>/<agent_id>/`: `*.md` prompt files, concatenated in
sorted filename order, and an `agent.yaml` manifest:

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

A run without `--agent` uses the `agent` setting: `set agent autocoder` in
`~/.js/jsrc` or a project `.js/jsrc`, or `JS_AGENT=autocoder`. The built-in
value is `defaultagent`.

Current prompt dirs:

- `defaultagent`: main orchestrator prompt. Selects core tools, `task`,
  `autocoder`, and `commit`.
- `autocoder`: headless engineering worker.
- `commit`: git commit worker.

## Shell Expectations

The `shell` tool runs commands with the `shell.program` setting:

`bash -o pipefail -c` by default. `set shell.program zsh` runs
`zsh -o pipefail -c`; any other program (such as `sh`) runs with `-c` and no
pipefail. A name is looked up on PATH; a path is used as given.

The Python harness does not itself require `fzf` or `bat`. `fs_search` invokes the
pinned `tools/bin/rg` installed by `just install`, falling back to PATH only
before that download has been run, and reports a plain ERROR when neither is
present. Agents can still call `rg`, `fzf`, or `bat` through `shell` when those
programs are installed and useful.

Use the `cwd` argument to the `shell` tool instead of embedding `cd` in the
command string.

## Sessions And Memory

Saved sessions are filed by the directory js started in:

```text
~/.js/sessions/<start-dir>/<session>.jsonl   the record
~/.js/sessions/<start-dir>/<session>.txt     a readable transcript
~/.js/sessions/<start-dir>/<session>/        its subagent runs
```

`<start-dir>` is the absolute path with `/` and `_` replaced by `-`. Use
`--session NAME` to continue a saved session from any directory, and `/name
<text>` to title one. A bare `js --session`, or `/session` in the REPL, opens
the session picker: every session newest first, with search. See [configuration-and-sessions.md](configuration-and-sessions.md).

The memory layer is append-only JSONL plus control marks:

- `session_reset`: future loads ignore earlier messages in that file.
- `rollback_to:N`: future loads truncate the loaded message list to `N`.
- `compaction:{...}`: future loads rebuild context as the unchanged system
  prompt, one `<compaction-summary>` user message, and a safe recent tail.
- `/wipe`: rotates the whole file to `.bak`, `.bak.1`, etc.

Use `/compact [focus]` in the REPL or `js --compact <session>` offline to append
a compaction mark without rewriting the JSONL file. `/compact -m <model>` makes
that one compaction with the named summarizer instead of `compact.model`.
Automatic cache-aware compaction is controlled by `set compact.auto` and the
`set compact.*` settings in `~/.js/jsrc` or project `.js/jsrc`.
