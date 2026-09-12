Tools that outlive the session: save a function to disk, load it back, keep its
revision history and its provenance.

{{#if kernel}}
The `kernel` tool gives you state for one session. This gives you state forever.
A function you write and save today is loadable by whatever model runs here
tomorrow, which can then refine it and save a new revision on top. The file
records who wrote each revision and why.
{{/if}}

Parameters:
- `action` (required): `list`, `load`, `save`, `history`, or `restore`.
- `name`: the tool's name, for `save`, `history`, and `restore`. It must be a
  plain Python identifier and it must match the name of the definition.
- `note`: for `save` — one line on WHAT CHANGED and why. This is the message the
  next model reads before deciding whether to trust or rewrite your version.
  A save without a note is close to useless.
- `scope`: `global` (default, available in every project) or `project` (stored
  under `.js/toolbox/` and only visible here). A project tool shadows a global
  tool of the same name.
- `revision`: for `restore` — the revision number to roll back to.
- `source`: for `save` — the definition text, only needed when the function is
  not in the kernel namespace.
- `verbosity`: `quiet`, `normal`, or `verbose` for the operator's terminal view.

`list` shows every saved tool with its current revision, its scope, and the
chain of models that have worked on it. A file that will not parse is listed as
`BROKEN` with the reason rather than being hidden.

A fresh box is not empty: `js` ships one example tool, `word_frequencies`, and
the first `list` or `load` copies it into the global toolbox. It is an ordinary
tool from then on — edit it, refine it, or delete it. Nothing overwrites a copy
that already exists, including a reinstall.

`load` execs every healthy tool file into the kernel namespace, then reports
what arrived. Each file is exec'd separately, so one broken tool costs you that
tool and nothing else. Call this once at the start of a session, before you
start writing code, so you build on what already exists.

Every file lands in one shared namespace, so a tool may call a sibling. A file
whose module-level code needs a sibling that has not loaded yet is retried after
the rest are in, so what a tool is named never decides whether it loads.

`save` reads the named definition out of the live kernel and writes it as the
next revision. It never overwrites: the previous revision is archived first and
stays restorable forever. A first save is `r1`; a save over an existing tool is
`r2`, `r3`, and so on, with your model name and note appended to the history.
Save when a function is worth having again — a parser, a fetcher, a report
formatter, anything you would be annoyed to rewrite.

Before writing, `save` hoists the definition's module imports into the file so it
stands alone, and refuses the save if the definition still reads a name the file
would not carry — a session constant, a sibling function, a client built in an
earlier cell. The ERROR names every one of them; save them as their own tools,
inline them, or pass a complete definition in `source`. Nothing is written and no
revision is bumped by a refused save. The same check runs on an explicit
`source`.

`history` prints every revision of one tool: date, model, note, and which
revisions can be restored.

`restore` rolls a tool back to an earlier revision. The old body is written as a
NEW revision rather than replacing the current one, so nothing is ever lost and
the rollback itself shows up in the history.

What belongs in the toolbox: self-contained functions and classes with clear
inputs and outputs. What does not: throwaway one-liners, anything holding a
credential, and anything whose behaviour depends on state left in one particular
session's namespace.

A saved tool that uses an async client owns closing it. Construct the client
inside the function, wrap the body in `try`/`finally`, and `await
client.aclose()` in the `finally`; a client left open keeps printing `Unclosed
client session` onto kernel stderr, which lands in a later result.
