# AGENTS.md

Guidance for coding agents working in this repository. `CLAUDE.md` is a symlink
to this file — one set of instructions, every agent reads the same thing.

## What `js` is

A personal terminal LLM harness in Python: one owner, one machine, no
customers, no production, no other developers. Interactive chat, one-shot
`-p`, pipe workflows, parallel subagents, wiki agents, commit-agent — built on
the Vercel AI Python SDK (`ai`). Bias: low friction, many settings. When the
owner says remove, it is gone — deleted, not renamed, no compatibility alias.

## Workflow

1. **`just` is the entry point.** `just` alone lists every recipe. uv owns the
   venv — `uv.lock` is the truth, so package changes go through uv; a
   `pip install` into `.venv` gets erased by the next `just sync`. Run js
   through `just run`, not `.venv/bin/js` (which goes stale).
   Dependencies are not hard-pinned with `==`. Keeping them current is a
   recurring chore: bump them, relock, run the suites, bump the version. An
   upper bound needs the incompatibility it guards against named beside it.
2. **Env feels broken? → `just sync`.** It is the real fix.
3. **Make the change in a worktree.** The `~/js` checkout stays on main: the
   `js` on PATH runs whatever it has checked out, so a branch left there
   changes the js the owner uses. Work on a branch in its own worktree:
   `git worktree add ~/.js/worktrees/js-<topic> -b <topic>`.
4. **`just lint`** before calling it clean. ruff is the gate.
5. **Done means merged, pushed and installed.** Merge the branch into main in
   `~/js`, push main to origin, then run `just install` there (it refuses to
   run from a worktree), so the `js` on PATH runs the change and the other
   machines get it with a pull.

## Where the suite runs

The owner works at yoda's screen, so the full suite does not run there:
`just test-on vader` copies the checkout to vader and runs it on vader's
cores (about two minutes). Focused runs (`just test-file`, one test) stay
local. `just test` locally takes a quarter of the cores; `JS_TEST_WORKERS`
names another count.

## Privacy 
- Keep our chat out of the files unless its relavent.
Commentary about having a hard drive go bad - No.
Commentary about technical discussions in which we arrived at a genuine shared conclusion and information is not a temporary state - Yes.

## Commands
`just` lists everything. The daily few: `just sync` (rebuild env — the fix
for a broken venv), `just run -p "..."` (run js; REPL with no args),
`just test` (offline suite), `just test-file <path>`, `just lint`,
`just check`. Focused suites exist (`just test-tools`, `test-wiki`,
`test-runtime`, …). Live tests (`ai_provider`, `e2e`, `vision`) need real
provider creds: `just test-live`. One test directly:
`uv run --extra test --extra browser pytest -q tests/test_foo.py::test_bar`

## Architecture in one breath

Streaming tool-use loop on one event loop, over the async SDK; tools are
sync and run on executor threads. `js/model_client.py` is the model I/O boundary (providers are registered in
`js/providers.py`); `js/runtime.py` is the loop (dispatch, subagent
fan-out), and a turn's model call and streamed reply live in
`js/turn_call.py` and `js/turn_stream.py`; `js/cli.py` is `main` (the command starts in `js/entry.py`): it reads
the command line (`js/command_line.py` defines it) and picks the mode, each
side mode (`--bench`, `--commit`, `--list-*`) living in a module of its own, and a command (`js generate`, `js init`, `js rotate`: a first argument named in the `js/commands.py` table) runs its own module's `main` with its own parser, `js/repl.py` is the interactive REPL (startup,
the input loop, the turn queue, steering), and `js/prompt_run.py` is the `-p`
run (its steps, the answer, the Continue line); `js/run_assembly.py` turns config,
agent and model into a settled run, the ready-to-run turn that `-p`, the REPL
at startup, `--bench` and swarm agents start from; `js/conversation.py` keeps
a session's history across turns, and `-p` and swarm agents run their turns
through it; `js/toolkit/` is the tools — model-facing contracts live
in `tool_descriptions/*.md`, not in code. Agents are prompt directories
(`js/persona.py` + `defaults/agents/`; the nearest layer's directory of a name replaces lower ones whole; `agent.yaml`
beside the prompt picks model, reasoning and tool surface, resolved against
`tools.yaml` noun:modifier rules). The project chain (`js/paths.py`
`project_root`/`project_dirs`): the root is the nearest `.git`, lifted out of a
submodule; each `.js/` from the root down to the start directory is a layer
over the home, nearest wins; no repository means home only. An untracked `.isolate` marker in a directory
or its `.js/` is a border (`paths.isolation`): nothing above it is read, the
home included, and the nearest marker wins; outside a repository it is a
root; `--isolate`/`--no-isolate` force or
ignore it per run. Config layers
`defaults/jsrc` < home `jsrc` < each chain `.js/jsrc` then `.js/jsrc.local` <
presets < env < `--extra`; `set -KEY` falls back to the layer below. Sessions are filed by project root (`paths.project_namespace`; the start directory outside a project), are append-only JSONL and
compaction leaves history intact. Inline-directive expansion
(`js/promptexpand.py`) is single-pass on purpose — that is the injection
guard; preserve it. Instructions: `JS.md`/`JS.local.md` from the home and
each chain `.js/`, and from each chain directory `JS.md` else `AGENTS.md` (and
the `.local` pair the same way). Nothing above the root is read. Deep dives belong in `docs/technical-guide.md`.

Stock setup lives in `defaults/`: jsrc, fset, agents, themes, tool descriptions,
skills, help, snippets, secret-patterns and the `js init` templates in `scaffold/`. A home file replaces only the same
relative stock file; an empty file shadows it too. Listings union names across
layers. Skills shadow whole directories by name. Only agents, skills, commands,
toolbox, tools.yaml and jsrc use the project chain. Missing named setup is a hard error.
`--home DIR` beats `JS_HOME`, then `~/.js`; the selected absolute directory holds
all configuration, sessions, logs, state, cache and tmp. Prompts and shell commands
receive `JS_HOME`; inline prompts also support `{{JS_HOME}}` and `%%JS_HOME%%`, and
`%%CURRENT_PROJECT%%`/`%%CURRENT_PROJECT_ROOT%%` for the project.

`--json` prints one final object; `--json-stream` streams typed JSON Lines.
Use `--list-models` or `--list-agents` for text or JSON; `--list-sessions`,
`--list-providers` and `--list-logins` require `--json`. Sampling flags
(`--temperature`, `--top-p`, `--top-k`, `--min-p`, `--top-n-sigma`,
`--repetition-penalty`, `--presence-penalty`, `--xtc-probability`,
`--xtc-threshold`, `--dry-multiplier`, `--dry-base`, `--dry-allowed-length`,
`--dry-penalty-last-n`, `--dry-sequence-breakers`) override `--extra`, agent
sampling and jsrc. A Continue command records the effective run options.

Anything settable is a registered setting (`js/settings.py`). `/set` with no
argument dumps every settable setting, `/set <key> <value>` sets one, and
`/save` writes to jsrc only what was changed in the session; nothing else
writes the user jsrc. A setting
reachable only through an env var or a hardcoded default is not finished.
A setting a turn reads is also one `TurnSetting` row in `js/turn_settings.py`;
the row makes it a `Config` field and, with `on_context`, a `ToolContext` field
that subagents inherit.

### Naming a setting

- A setting is a global variable, named UPPER_SNAKE: `set BANNER ***`.
- Spell words out. No clipped abbreviations (`CTX` is wrong). `ENV`, `URL`
  and `API` are fine.
- A cap ends `_MAX`, as in C's `limits.h`. A timeout that kills ends
  `_TIMEOUT`, in seconds. A wait that returns a handle ends `_WAIT`, in
  seconds.
- One file ends `_FILE`, a directory ends `_DIR`, so `/set *_FILE` lists them.
- Setting `NAME` is also env var `JS_NAME`.
- When in doubt, do it the way bash, unix, ircII, C or classic tooling does.
- js output never says "knob" or "folder". Say setting, and directory.

## Docs

Deep dives belong in `docs/`: reference material, benchmark results, design
write-ups. If you produce that kind of thing, it goes there.

Trust code over docs. Docs go stale; the code is what runs every day, and
nobody here rereads `docs/` to keep it current. When a page in `docs/`
disagrees with the code, the code is right. If you are changing the area a
stale page describes, correct or delete the stale text in the same change.

Issues are beads: `bd ready` for open work, `bd show <id>` for the spec
pointer and acceptance, `bd create` to file, `bd close <id>` when done,
`bd sync` after a batch. Never `bd delete` a child id (`js-x.N`) — it can
take the parent epic with it. Design docs live in `~/.js/work/`.
`.scratch/` is for a reproduction you are still building, not a backlog.
Leave `toolsweep-*` artifacts alone unless explicitly asked to clean them up.

## Tests and comments

Tests pin what must be true for the code to be correct. Do not assert on
wording, presentation, or styling that could change without anything being
wrong.

Comments state what is true about the code. Do not turn something someone
said once into a rule in the code, and do not describe what the code does not
do or alternatives nobody asked about. Describe what is there.
