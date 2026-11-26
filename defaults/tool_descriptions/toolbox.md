Tools that outlive the session: save a function to disk, load it back, keep its
revision history and who wrote each revision.

{{#if kernel}}
The kernel tool gives you state for one session. This gives you state for
good. A function saved today is loadable by whatever model works here
tomorrow, which can refine it and save a new revision on top.
{{/if}}

Build tools here, and build them specialized. A tool that knows this project's
log format, this API's pagination and rate-limit quirks, this repo's release
steps in order, or the query that answers the question the owner keeps asking
is the point of the toolbox. It does not need to be general or reusable
elsewhere; a tool that does one job for one project and gets it right is
worth more than a general one that needs re-explaining every session.

A workflow is a tool too. A saved tool may call js tools through
`tools.<name>(...)`, using the short js names that `tools.names()` lists in a
cell, so one function can run a project's steps in order, shell commands and
file edits included, and check each one.

Save the first time something works. If you wrote a function and it did its
job, save it in the same turn, before moving on. Saving is cheap and never
destructive, and the next session refines a rough `r1` far faster than it
rediscovers the edge cases from nothing. Load at the start of every session
and use what is there before writing anything new; when a saved tool is wrong
or incomplete, fix it and save the next revision.

Parameters:
- `action` (required): `list`, `load`, `save`, `history` or `restore`.
- `name`: the tool's name, for `save`, `history` and `restore`. A plain Python
  identifier that matches the name of the definition.
- `note`: for `save`, one line on what this revision does or changes, and why.
- `scope`: for `save`. `project` writes to the project's `.js/toolbox`, which
  is where a tool that knows this project belongs, so pass `scope=project`
  for those. `global` is for tools that are useful in any project, and is
  what an omitted `scope` means. When both hold a tool of the same name, the
  project one wins.
- `revision`: for `restore`, the revision number to roll back to.
- `source`: for `save`, the definition text, when the function is not in the
  kernel namespace.
- `verbosity`: `quiet`, `normal` or `verbose`, for the operator's terminal view.

Make a tool findable. `list` and `load` print one line per tool: its name,
revision, scope and the models that have worked on it. The name is what the
next model sees first, so make it say what the tool is for:
`parse_acme_access_log`, not `parse_log`. Give every tool a docstring whose
first line says what it does and when to reach for it, then the arguments and
what comes back; after a `load`, `help(<name>)` shows it.

The `note` is the history line the next model reads before trusting or
rewriting a revision. "fixed it" tells it nothing; "off-by-one on the last
chunk when the file has no trailing newline" does.

`load` execs every tool file into the kernel namespace and reports what
arrived. Nothing saved is in the namespace until it has run. Each file is
exec'd separately into one shared namespace, so a tool may call another saved
tool, and a file that fails is listed `BROKEN` with the reason while the rest
still load. A fresh toolbox holds one shipped example, `word_frequencies`.

`load` and `save` run in the kernel, so they need it idle: while a cell is
still running they return an `ERROR` naming its handle instead of queueing
behind it.

`save` reads the named definition out of the live kernel, carries its imports
into the file (including the module and alias of a `from` import), and writes
it as the next revision: `r1`, then `r2`, and so on.
It never overwrites. The previous revision is archived and stays restorable,
so saving over an existing tool is safe.

A saved file has to stand alone, so `save` refuses a definition that reads a
name the file would not carry: a session constant, a client built in an
earlier cell, an unsaved helper. The `ERROR`
lists every such name and nothing is written. Fix it by taking the value as a
parameter, building it inside the function, or saving the helper as its own
tool first. Calling another saved tool is fine.

A tool that needs a secret reads it from the environment when it is called,
`os.environ["ACME_TOKEN"]`, so the file carries the variable's name and never
the value.

A tool that uses an async client builds it inside the function and closes it:
`try`/`finally` with `await client.aclose()`. One left open prints `Unclosed
client session` into later kernel results.

`history` prints every revision of one tool: date, model, note, and which
revisions can be restored. `restore` writes an earlier body as a new revision,
so the rollback is itself in the history and nothing is lost.
