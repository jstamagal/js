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
A cell body runs ON the loop, so a cell that blocks waiting for that same loop
to finish something never completes. `run_coroutine_threadsafe(coro, loop)
.result()` is the usual shape, and a nested `loop.run_until_complete(...)` under
a loop patch is the same mistake: the thread that would run `coro` is the thread
you just blocked. This is not a slow call and not a timeout you can raise your
way out of — an instant `async def q(): return 7` hangs exactly as long as a
60-second download, and `.result(timeout=n)` reports `TimeoutError` after `n`
seconds whatever the coroutine does. `await` is the only form that runs: it
yields the loop instead of blocking it. A sync wrapper around an async client
fails here for this reason even when the server is healthy, so call the async
method directly rather than reaching for the library's sync shim.

The tool stays usable throughout: the submitting call returns a handle, the next
`code` is refused with `the previous cell is still running`,
`action="interrupt"` stops the blocked cell, and the next cell's `await` is
served normally.
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
- Cells run inside a live asyncio event loop. `await` works at cell top level,
  so `async def` helpers are awaited directly; `asyncio.run(...)` and
  `loop.run_until_complete(...)` raise `RuntimeError: This event loop is already
  running` there, and `await` is the fix. Patch nothing: loop-patching libraries
  are not needed, and `import nest_asyncio` fails in a cell.
- Close the async clients you open. Construct the client inside the function,
  wrap the work in `try`/`finally`, and `await client.aclose()` in the `finally`.
  A client left open keeps printing `Unclosed client session` onto kernel stderr,
  and that reaches you in a later result.
- Cells run in the interpreter that runs js: the uv tool environment of a
  `just install`-ed `js`, the project venv under `just run`. Modules installed
  in some other project's venv are not importable here, so check
  `sys.executable` in a cell and install into that one:
  `uv pip install --python <that path> <package>`. `!pip install` and other
  IPython magics reach the same environment.

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
