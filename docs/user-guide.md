# User Guide

`js` is a terminal agent. You give it a prompt, it sends a turn through the
Vercel AI Python SDK (`ai-python`), the model can call local tools, and the
runtime loops until the model returns a final answer or hits a stop condition.
## Install

```bash
pip install -e ".[test]"
```

The package exposes two scripts:

```bash
js
js-drain
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
/set [key [val]]              list knobs, show one, or change one
/show [key]                   list all current config values or one key
/set model.reasoning_effort high
/set compact.auto off
/turns
/persona
/session
/reset
/wipe
exit
quit
:q
```

Inside the `/model` picker, press `:` for command mode. The picker lists saved
logins, not every possible SDK provider. `js --logout <provider>` removes that
provider and its cached models from the picker.


```text
:/provider my-proxy
:/key sk-...
:/url http://127.0.0.1:11434/v1
:/openai-completions
:/openai-responses
:/anthropic
:/codex
:/ollama
:/llama.cpp
:/mimo
:/mimo-token-plan
:/fetch
:/login
```

`/reset` clears the in-process conversation and writes a `session_reset` mark to
the JSONL so future loads ignore older messages in that file.

`/wipe` rotates the active session file to `.jsonl.bak`, `.jsonl.bak.1`, and so
on, then clears the in-process messages.

Ctrl-C while a turn is running rolls back the appended user message in memory
and writes a rollback mark. Runtime exceptions do the same.

## One-Shot Prompt Mode

```bash
js -p "write a short repo summary"
```

One-shot mode appends the user prompt to the session, runs one turn, prints the
final assistant message, persists new messages, and prints a `Continue:`
command when saving is enabled.

Useful flags:

```bash
js -p "prompt" --no-save
js -p "prompt" --session 20260611T120000Z-abcd
js -p "prompt" --debug
js -p "prompt" --debug-file /tmp/js-debug.log
js -p "prompt" --reasoning off
js -p "prompt" --max-out 64000
js --migrate-config
```

`--debug` streams the trace to stdout. `--debug-file` writes the rich trace to a
file and keeps stdout clean. They are mutually exclusive.

`--migrate-config` runs the one-shot legacy-to-`jsrc` conversion and exits; see
[Configuration And Sessions](configuration-and-sessions.md) for the file-level
details.

## Pipe Mode

If stdin is not a TTY, `js` reads stdin as prompt input:

```bash
git diff | js -p "review this patch"
cat notes.md | js
```

When `-p` is also supplied, the final prompt is:

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

## Wiki Mode

Wiki mode uses built-in prompts and native `wiki_*` tools:

```bash
js --wiki=ingest --vault=creative ~/notes/source.md
js --wiki=ingest,synthesize --vault=general ~/papers/paper.pdf
js --wiki=query --vault=creative "what does the wiki know about X?"
js --wiki=lint --vault=general
```

Modes can be comma-separated. Each mode runs as a separate kickoff turn in one
shared session so `ingest,synthesize` does not collapse into one overloaded
prompt.

Vault aliases:

```text
creative -> ~/wiki-creative
general  -> ~/wiki-general
```

See [Wiki Mode](wiki.md).

## Artifact Mode

Artifact mode uses built-in prompts and native `artifact_*` tools around the
external `artifact` CLI:

```bash
js --artifact=curate
js --artifact=curate,digest
js --artifact=query "find the handoff for the artifact work"
js --artifact=lint
```

See [Artifact Mode](artifact.md).

## Drain

`js-drain` runs sequential wiki ingest jobs over a vault inbox or arbitrary
folder. It packs small files, splits large files, and can archive originals
after successful jobs.

```bash
js-drain creative
js-drain creative -a
js-drain creative -f ~/dump
js-drain creative -n
js-drain creative -l 3
```

See [Drain](drain.md).

## Prompt-Directory Agents

Agents are discovered from repo `prompts/`, global `agents/` in the platform config dir, and
project `.js/agents/`; project scope wins over global, which wins over repo.
Each agent lives at `<root>/<agent_id>/*.md`. Files are concatenated in sorted
filename order. The first `00*.md` file can include YAML frontmatter:

```markdown
---
tools:
  - read
  - write
  - fs_search
  - patch
  - task
---

System prompt body.
```

Selectors can be exact tool names, glob patterns such as `todo_*`, or `*` for
the whole registry. No selected tools means the model gets no tools.

Current prompt dirs:

- `defaultagent`: main orchestrator prompt. Selects core tools, `task`,
  `autocoder`, and `commit`.
- `autocoder`: headless engineering worker.
- `commit`: git commit worker.

## Shell Expectations

The `shell` tool uses the configured operating-system shell:

- Unix: `$SHELL -c`, falling back to `/bin/sh -c`.
- Windows: `COMSPEC /C`.

If the environment starts with `SHELL=/usr/bin/zsh`, `shell` is zsh-first. The
Python harness does not itself require `fzf` or `bat`, and `fs_search` is a
Python regex implementation rather than a subprocess call to `rg`. Agents can
still call `rg`, `fzf`, or `bat` through `shell` when those programs are
installed and useful.

Use the `cwd` argument to the `shell` tool instead of embedding `cd` in the
command string.

## Sessions And Memory

Saved sessions live under the platform data directory:

```text
<data-dir>/sessions/<agent_id>/<session>.jsonl
```

Each agent id has isolated session state. A `wiki` session is not a
`defaultagent` session. Use `--session` to continue a specific saved session.

The memory layer is append-only JSONL plus control marks:

- `session_reset`: future loads ignore earlier messages in that file.
- `rollback_to:N`: future loads truncate the loaded message list to `N`.
- `compaction:{...}`: future loads rebuild context as the unchanged system
  prompt, one `<compaction-summary>` user message, and a safe recent tail.
- `/wipe`: rotates the whole file to `.bak`, `.bak.1`, etc.

Use `/compact [focus]` in the REPL or `js --compact <session>` offline to append
a compaction mark without rewriting the JSONL file. Automatic cache-aware
compaction is controlled by `set compact.auto` and the `set compact.*` knobs in
platform `jsrc` or project `.js/jsrc`.
