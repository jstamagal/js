# Sessions, the ~/.js home, and the fixes around them

Design agreed with the operator on 2026-09-29. Nothing here is built yet.
Beads: epic js-1g1.

The harness is for the agent. Layouts and formats here are chosen first for
an agent working with ripgrep and head; the operator's picker reads the same
files.

## 1. The ~/.js home migration, done right

The first migration (js-l7e.1) moved `~/.config/js`, `~/.local/share/js` and
`~/inbox/agents/js` into `~/.js` on the first js start. It was wrong in three
ways:

- It moved the agents but did not convert them. js on the same commit
  refuses `00-tools.yaml`, so every agent stopped loading until a separate
  `just migrate-agents` ran.
- The agent conversion (`scripts/migrate_agents.py`) copied tool names
  verbatim. Tools js deleted long ago (`multi_patch` in 23 agents,
  `sem_search` in 23, `grep` in 9, `serve`, `followup`, `artifact_*`) landed
  in `agent.yaml` as eager entries, and js warns about each on every load.
- A relative symlink moved to a directory at a different depth points
  somewhere else afterwards (yoda's `agents/research` was
  `../../../darkstar/...`).

The automatic migration stays. The fixed one:

1. Moves each old location into the layout below, as today.
2. Converts every moved agent to `agent.yaml` in the same step. A tools
   entry is kept only when it matches a tool: a `tag:` entry, a glob that
   matches at least one tool, or a name that is a tool or an agent in the
   roots being loaded. Every dropped entry is printed, per agent.
3. Rewrites a moved relative symlink so it resolves to the same absolute
   target it did before the move.
4. Re-files sessions by start directory, puts subagent runs under their
   parent, and writes each session's `.txt` transcript (see §2). Doing it
   here means sessions move once. Migrated sessions keep their file names,
   so `--session <old name or hash tail>` still resolves.
5. Prints one line per move, as today, and writes the marker.

Separately, `paths.ensure_home()` creates every directory of the layout on
each start, so `ls ~/.js` always shows the whole structure:

    ~/.js/
      jsrc  JS.md  tools.yaml  tags.yaml     files; not created, only read
      agents/  skills/  toolbox/
      logins/        credentials and the model-list cache
      sessions/      every session, flat
      state/         undo store, kernel output, spilled results, commit backups
      logs/          logs, transcripts
      cache/         models.dev catalog, env probe cache, session search index
      work/          agent keepers (was ~/inbox/agents/js); `:n` notes
      tmp/           agent junk; entries older than a day are cleared on start
      plans/         plan tool output
      probes/        browser screenshots, terminal snapshots

### Redoing it on the three hosts

- **vader:** old layout already restored. Delete the copies placed in
  `~/.js` (`agents/`, `sessions/`, the marker), then one start of the fixed
  js migrates for real.
- **yoda, darkstar:** migrated by the broken code. Restore the old layout
  (the moves are listed in the first run's output; `00-tools.yaml` files come
  back from `~/.js-backups/pre-js-home-20260929.tgz`), then one start of the
  fixed js.
- The operator sees each host's migration output.

### Agent prompts

Prompts that name the old locations are updated to the new ones, on each
host. On vader:

| where | change |
|---|---|
| `defaultagent`, `js/defaultagent`, `bench69`, `toolaudit`, `testk`, `notools`: the "🦍 own mess splits two ways" line | the new stock wording: junk to `~/.js/tmp`, keepers to `~/.js/work` |
| 3 agents naming `~/inbox/agents/js` | `~/.js/work` |
| 1 agent naming `~/.local/share/js/sessions` | `~/.js/sessions` |
| `xlate`…`xlate3` (`/tmp/prompt-XXX.md`), `spec-*` and `pre-build` (`/tmp/specpipe`), `findholes` (`/tmp/fork.html`) | `~/.js/tmp/...` |
| `bench69/01-benchmark.md` (`/tmp/01.html`) | unchanged: it is a benchmark task |

yoda's and darkstar's lists are gathered and shown before editing.

## 2. Sessions

### Storage

Sessions are filed by the directory they were started in, as Claude Code
does. The directory name is the start path with `/` and `_` replaced by `-`;
the exact path is in the session header.

```
~/.js/sessions/
  -home-ronald-rump/
  -home-ronald-rump-js/
    2026-09-29T0802-6d65.jsonl     the record (source of truth, append-only)
    2026-09-29T0802-6d65.txt       readable transcript, regenerated from the jsonl
    merrygoround6969420.jsonl      a named session, filed where it was created
    merrygoround6969420.txt
    2026-09-29T0802-6d65/          that session's subagent runs
      task-1789016792-c0d9.jsonl
  -home-ronald-rump-js-js-toolkit/
```

- An agent in `~/js/js/toolkit` greps
  `~/.js/sessions/-home-ronald-rump-js-js-toolkit/*.txt`. A prefix glob
  (`-home-ronald-rump-js*`) covers a whole tree, like `dir:~/js/**`.
- New generated names are `YYYY-MM-DDTHHMM-xxxx`: readable, short, and `ls`
  order is time order. Named sessions keep their names.
- Subagent runs live in a folder named after their parent, so a plain grep of
  a project folder does not hit them.

**The `.txt` transcript**, rewritten whenever the session changes:

```
agent: defaultagent   dir: /home/ronald_rump/js   mode: repl
models: deepseek-v4-flash → xiaomi/mimo-v2.6-pro (#0031)
started: 2026-09-29 08:02   last: 2026-09-29 11:40   turns: 41
branched-from: -
tags: js · linux admin

#0001 08:02 you  APE
#0014 08:31 you  the spilled result is one line, read can't get past it
#0015 08:31 tool:shell  🦍 look at the spill file  $ wc -lc result-8716….txt  → exit 0, 91984B
#0016 08:32 ape  the file has no real newlines, it's escaped JSON…
```

- A fixed header: `head -6 *.txt` summarises a folder.
- One numbered message per line (multi-line messages continue indented), so
  `rg -n` hits are readable and citable as `#0014`.
- Tool calls show their label (the model's accompanying text), the command's
  first line, exit and size. Output is not in the `.txt`; it is in the
  `.jsonl` at the same message number.

**The record** (`.jsonl`):

- **Every assistant message carries a stamp:** model, provider, reasoning
  level. Resume uses the last stamp, so a session resumes on the model it
  was last talking to, not the one it started with. Today only session
  starts record the model; `/model` mid-session writes nothing.
- The session records how it was started: `repl`, `-p`, or piped, plus the
  command line.
- A branch records its parent session and the message index it split at.
- `/name <text>` appends a pinned title record. There are no model-written
  titles: the operator's first message is nearly always "APE", and
  generated titles in other harnesses are usually wrong.

**`--session`:**

- `js --session` alone opens the picker.
- `js --session NAME` creates or resumes a session by name, as today; scripts
  rely on it. Lookup is the current directory's folder first, then every
  folder. A name found in two folders is refused with both paths listed.
- A generated name or a unique tail of it (`--session 6d65`) resumes, as today.

### Session kinds

- **Empty:** the operator said something and nothing came back. Hidden.
- **Quick:** one operator message, at most 2 tool calls, a final reply under
  about 1,000 characters (`js -p "foo"`, `wyd`, "whats my GPU temp"). Not
  tagged, hidden by default.
- **Subagent and script-started** (subagent children, the wiki pipeline,
  the commit agent): hidden by default.
- Everything else is shown.

`a` shows all of them. Quick sessions carry a `quick` marker there, and
`mode:quick` lists only them.

### The picker

Opened by `/session` in the REPL or a bare `js --session`. It never opens on
its own.

Default screen:

```
 SESSIONS  188 · flat · newest first · all dirs          v=view  /=search  b=messages  i=info  a=all  esc=close
  when          mode  agent             dir                          turns  length  tags
> Sep 29 09:14  -p    defaultagent      ~/js                            1    40s    js
• Sep 29 08:02  repl  defaultagent      ~/js                           41    3h     js · linux admin
  Sep 17 16:50  repl  defaultagent      ~/.local/share/js/sessions/…    8    10m    nfs / mounts · remote hosts · networking
  Sep 17 10:01  repl  defaultagent      ~/js                           45    8h     terminal / console · linux admin · hardware
  ├─ Sep 17 11:40     ⎇ from #0031                                     12    40m    terminal / console
```

- Newest first, cursor on the newest. Every directory and every agent in one
  list. `•` marks sessions started in the current directory.
- Branches are nested under the session they came from.
- **Enter** resumes the highlighted session with its own agent, directory
  and last-stamped model.
- **`v`** cycles views: flat; grouped by directory (`[~]`, `[~/js]`, …);
  grouped by agent (`[defaultagent]`, …).
- **`b`** opens the message list:

  ```
   MESSAGES  Sep 17 10:01 · defaultagent · ~/js · 45 turns       enter=branch here  r=resume at end  esc=back
    #0029 10:44:09 tool:shell | 🦍 sniff the fbcon timings, see what the kernel thinks
    #0030 10:44:14 ape        | fbcon redraws the whole 4K buffer on each switch — that's the lag.
  > #0031 10:47:30 you        | ok so can we make it faster
  ```

  Assistant rows show the model stamp where it changes. Enter branches at the
  highlighted message. Esc returns to the list with the position kept.
- **`i`** shows the file path, model stamps, token count and branch parent.
  The file name appears nowhere else.
- **Tool rows** are labelled with the text the model wrote alongside the
  call (as bitchtea does). With no such text: the tool name and the first
  line of the command.

### Search

`/` takes a query. Filters narrow the list; the remaining words are ranked
with BM25 over what the operator said, what the model said, and tool-row
labels. Tool output is not indexed. The line under the query shows how it
was parsed. Each hit shows the line that matched. Quick sessions are
searched only while `a` is on.

| query | meaning |
|---|---|
| `niri motherboard` | BM25 over the conversation text |
| `>10`, `<2`, `>=10,<=20` | message count |
| `today` `yesterday` `week` `2026` `2025` `2026-09` | date; `today:niri` = `today niri` |
| `agent:defaultagent`, `agent:*research*` | agent, glob |
| `dir:~/js` | started in exactly `~/js` |
| `dir:~/js/*`, `dir:~/js/**` | one level under / anywhere under `~/js` |
| `mode:-p`, `mode:quick` | how it was started |
| `model:*qwen*` | any message stamped with a matching model |
| `tag:nfs` | tag |

Terms combine with AND. The index is an SQLite FTS5 table under
`~/.js/cache/`, updated as sessions are written.

### Tags (Jev)

Each non-quick session gets up to three tags from an operator-owned list in
`~/.js/tags.yaml` (one line per tag: name and a short description).

- One TypeSafe request per session: `state` = the last 10 operator and model
  messages (tool output excluded); one `noul` question per tag ("does the
  conversation have this as one of its main subjects").
- Tags scoring ≥ 0.6 are kept, highest first, at most three.
- Computed when a session ends. Retagged when `tags.yaml` changes.
- Tags describe subjects only. Activity tags such as "something broke" or
  "coding" fire on nearly every session (the operator starts js because
  something broke, and the coding is the fix), so they are not in the list.
- Cost measured on 20 real sessions with 22 tags: ~3,200 input tokens and
  ~200 ms per session; all 188 visible sessions ≈ $0.03 at $0.042 per Mtok.
- The conversation text is sent to api.typesafe.ai
  (`TYPESAFE_API_KEY`). TypeSafe states requests are not used for training.

Starting list (from the 2026-09-29 test): js, bitchtea, other harnesses
(opencode, pi, codex, claude code, crush, forge; installing or upgrading
them), gpu / vram, hardware, llm serving, model eval, quantization,
networking, nfs / mounts, remote hosts, linux admin, terminal / console,
web / html, image gen (AI-generated images, not SVG), video gen, prompts /
personas, creative writing, personal life, banter.

## 3. Mode-switch note

When a session continues in a different mode from the one its last turn ran
in (`-p` → REPL or REPL → `-p`), js attaches one `<js-reminder>` to the next
operator message: "This conversation started as a one-shot run and now
continues in interactive chat. The human is here and can answer." (and the
reverse).

Why: on 2026-09-29 a `-p` session resumed in the REPL kept acting one-shot.
The system prompt said `mode=repl`, but the replayed reasoning from the `-p`
turn said "one-shot mode", and the model extended it to "do not chain shell
calls", a rule that exists nowhere in js. The only one-shot rule js sends is
`tools/envctx.c:1017` ("NEVER end the turn on a question").

## 4. Shell tool

- New setting `shell.program`, default `bash` in `js/jsrc`. Today the tool
  runs `$SHELL` with no setting; models write bash, and zsh broke a model's
  `echo ===` separator on 2026-09-29.
- Under bash, commands run with `pipefail`, so `dmesg … 2>/dev/null | rg … |
  tail` reports dmesg's failure instead of `exit 0` with no output.
- The `environment=filtered allowed=… are unset` preamble is added to a
  result only when the command referenced a filtered variable.

## 5. Display

- The per-turn `run model=… provider=…` header stays.
- `ui.net` default stays 2; `set ui.net 1` in jsrc shows failures only.
- Bug: the last block of a streamed answer was redrawn garbled ("reseat
  607:00scaed if width stays x4.glips smack", yoda, 2026-09-29, session
  `20260929T101208214271Z-acc53bcfc3ea7213`). Compare the screen against the
  session file, find the live-block redraw fault in `js/display.py`.

## 6. `-C DIR` keeps the agent in DIR

Before js-1g1.10 `-C` only `chdir`ed. On 2026-09-10 an audit turn "confined" with `-C
/tmp` read `~/.zshrc` and sent four live API keys to a remote endpoint
(commit `59bc8cb`); the fix only jailed the bench scripts. The goal here is
not hostile-model security (that is `~/src/llmbench`'s job) but keeping the
agent's context clean: no `find /` or `rg ~` sweeps through the operator's
home.

- Every command tool (shell, kernel, terminal, toolbox) runs under
  bubblewrap. DIR is bound read-write at its real path and is the working
  directory.
- The operator's home is a tmpfs. Bound back read-only: every `$PATH`
  directory under home, so toolchains work. The setting `jail.bind` adds
  paths (`~/.cache/uv:rw`, …).
- `/usr`, `/etc` and the rest of the system: read-only. `/tmp` and
  `~/.js/tmp`: private to the jail.
- The network stays on. Provider keys are not in the tool environment.
- read / write / patch / remove / fs_search resolve paths and refuse anything
  outside DIR (and the bound paths) with one line.
- Subagents inherit the jail. No bwrap on the box: js refuses `-C` with one
  line; there is no unjailed fallback.
- `-C` is the jail; there is no separate flag. A plain working directory is
  `cd DIR && js`. The operator always read `-C` as a jail.
- `/cd DIR`, with or without `-C`, moves the session's working directory (js's
  and the tools'). Under `-C` it goes only to DIR or a bound path; elsewhere it
  is refused. It puts one `<js-reminder>` on the next user message ("working
  directory is now DIR") and writes a `workspace:` mark (root, cwd). A resume
  restores the cwd.

How it is built (`js/jail.py`):

- One jail per js process, entered by `-C` after a self-test (`bwrap … true`).
  Each command builds its own bwrap argv, so a changed `jail.bind` reaches the
  next command.
- `--ro-bind / /`, then empty tmpfs mounts over `/home`, the operator's home
  (by `$HOME` and by the password database), `/run/user`, every network
  filesystem mount, and every other mount that shows one of those (a home on a
  btrfs subvolume is also under the mount of the whole filesystem). Other
  users' homes on this box alias the operator's, so all of `/home` goes.
- `/tmp` and `~/.js/tmp` are directories under `~/.js/state/jail/<pid>-…`,
  bound in the jail and also at their own path. They last for the js
  process, so a file one command leaves in `/tmp` is there for the next. The
  file tools map `/tmp/…` and `~/.js/tmp/…` to them.
- PATH directories under a hidden tree are bound back with every symlinked
  directory on the way (`jail.reach`), so a venv interpreter that links into
  `~/.local/share/uv/python` still starts.
- No pid namespace: a command's background server outlives the command, as
  outside the jail. `--unshare-ipc --unshare-uts --unshare-cgroup-try`, network
  shared, `--die-with-parent`.
- The kernel starts through `sh -c 'trap "" INT; exec "$@"'`: its interrupt
  SIGINTs the process group, which holds bwrap, and bwrap would die of it.
- The file tools go through `ToolContext.resolve_path`, which asks the jail;
  a `JailError` becomes one `ERROR:` line in `call_tool`. Writes need a
  read-write area (DIR, a `:rw` bind, the private tmp).
- `JS_JAIL` is exported; `tools/envctx.c` prints `confined=DIR` and a rule
  line. It prints `shell=$JS_SHELL`, which js exports from `shell.program`
  before expanding the system prompt.
- Not jailed: MCP servers (the operator configures them), the commit helper's
  git (js's own code), inline prompt directives (the operator's prompt).

## Open

- Probes that failed with no reply (a 402, a DNS error): shown with the
  error and model (`✗ 402 deepseek-v4-flash`), or hidden with the empty
  sessions?
- Tags as a fourth `v` view (`[nfs / mounts]`, …) or only a column and filter?
