Execute Python in a persistent IPython kernel. State survives between calls.

One kernel runs for the whole session. Everything a cell defines — functions,
classes, imports, open connections, loaded dataframes — is still there on the
next call and every call after it. Define a helper now, call it twenty turns
later. This is the point of the tool: build your own instruments as you go
instead of re-deriving the same work in every cell.

Parameters:
- `code`: the Python to run. Empty `code` runs nothing and just reports the
  current namespace.
- `action`: `run` (default) submits `code`; `poll` returns what a submitted cell
  has produced since it was last read; `interrupt` SIGINTs the cell; `wait`
  blocks for it.
- `handle`: the id of the cell a `poll`, `interrupt`, or `wait` acts on.
  Defaults to the cell that is running, or the one submitted last.
- `timeout` (default `120` seconds): the longest one call blocks — both the wait
  when a cell is submitted and the block an explicit `wait` performs.
- `restart` (default `false`): kill and restart the kernel. This DESTROYS every
  definition and every value in the namespace. Only use it when the kernel is
  reported dead, or when you deliberately want a clean slate.
- `verbosity`: `quiet`, `normal`, or `verbose` for the operator's terminal view
  of this one call. It changes nothing about what you receive.

A cell does not have to finish before you get an answer. `kernel` submits it and
waits a few seconds; a cell that finishes inside that window comes back whole,
and a cell still running comes back as a handle:

    cell 3 is still running after 5.0s (handle 3). Poll it with action="poll" ...
    HANDLE 3 RUNNING

Then `action="poll", handle="3"` returns what it produced since the last read
and whether it is still running, `action="wait", handle="3", timeout=120` blocks
for the rest, and `action="interrupt", handle="3"` stops it. Output produced
while you are off doing something else is kept and delivered on the next poll.

Every result for a finished cell ends with a `NAMESPACE` line listing the
functions and classes the session defined (`NAMESPACE (none)` when it defined
none), and a `DEFINED` line for anything this cell added — functions, imports,
and values alike. That listing is re-derived from the kernel itself on every
call, so it is accurate even when the conversation that defined a function is no
longer in your context. Read it. It is the record of what you have already
built. A cell that is still running ends with `HANDLE <id> RUNNING` instead, and
a new `code` submitted while it runs is refused rather than queued behind it:

    ERROR: the previous cell is still running (<first line>); interrupt it or wait

Output behavior:
- stdout, stderr, `repr` results, and tracebacks all come back. A cell that
  raises returns the real traceback, not a summary.
- Images produced by matplotlib and friends are written to files under
  `.js/kernel/` and reported as `IMAGE <path>`.
- The whole result is capped by `limits.max_tool_result_bytes` with a visible
  truncation marker. Do not shrink output by hand; let the cap do it.

Interrupting does not destroy your work. A `KeyboardInterrupt` — from a `wait`
that ran out of time, from `action="interrupt"`, or from Ctrl-C on the turn that
submitted the cell — stops the cell exactly like Ctrl-C in a notebook: the
namespace and everything in it survive, and the result carries the traceback.
The one cell SIGINT cannot stop is one blocked in a syscall that ignores it, a
network call stuck on a dead resolver being the usual case; only
`restart=true` clears that, and a later call reports the cell as still running
instead of queueing behind it and waiting out its timeout.

If the kernel process actually dies (a segfault, an `os._exit`, the OOM killer)
the result says so plainly and names the cell. That is the one case where
everything is gone and `restart=true` plus a rebuild is the answer.

Practical notes:
- Long-running or exploratory work belongs here rather than in one-shot
  scripts: you keep the intermediate state.
- Prefer defining a named function over pasting the same block twice. A named
  function shows up in `NAMESPACE` and stays callable.
- `!pip install` and other IPython magics work, but installing into the running
  environment is rarely what you want.

{{#if shell}}
Use `shell` instead for builds, tests, git, package managers, and anything that
is really a command rather than Python. `shell` gets you a fresh process every
time; this tool gets you a process that remembers.
{{/if}}

{{#if toolbox}}
This tool does not persist anything past the session. `toolbox` is the layer
that does: `toolbox action=load` pulls previously saved tools into this kernel's
namespace, and `toolbox action=save name=<fn>` promotes a function you defined
here to disk with provenance. If you write something worth having tomorrow,
save it — otherwise it dies with the session.

At the start of a session, call `toolbox action=load` once before writing new
code, so you do not rebuild something a previous session already got right.
{{/if}}
{{#unless toolbox}}
Nothing here survives the session. When this session ends the kernel is torn
down and every definition in it is gone. Write code accordingly: if something
must outlive the session, write it to a file.
{{/unless}}
