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

`load` execs every healthy tool file into the kernel namespace, then reports
what arrived. Each file is exec'd separately, so one broken tool costs you that
tool and nothing else. Call this once at the start of a session, before you
start writing code, so you build on what already exists.

`save` reads the named definition out of the live kernel and writes it as the
next revision. It never overwrites: the previous revision is archived first and
stays restorable forever. A first save is `r1`; a save over an existing tool is
`r2`, `r3`, and so on, with your model name and note appended to the history.
Save when a function is worth having again — a parser, a fetcher, a report
formatter, anything you would be annoyed to rewrite.

`history` prints every revision of one tool: date, model, note, and which
revisions can be restored.

`restore` rolls a tool back to an earlier revision. The old body is written as a
NEW revision rather than replacing the current one, so nothing is ever lost and
the rollback itself shows up in the history.

What belongs in the toolbox: self-contained functions and classes with clear
inputs and outputs. What does not: throwaway one-liners, anything holding a
credential, and anything whose behaviour depends on state left in one particular
session's namespace.
