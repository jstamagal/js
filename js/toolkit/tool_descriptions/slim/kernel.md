Execute Python in a persistent IPython kernel. State survives between calls.

One kernel runs for the whole session. Everything a cell defines — functions,
classes, imports, open connections, loaded dataframes — is still there on the
next call and every call after it. Define a helper now, call it twenty turns
later. This is the point of the tool: build your own instruments as you go
instead of re-deriving the same work in every cell.

Parameters:
- `code`: the Python to run. Empty `code` runs nothing and just reports the
  current namespace.
- `timeout` (default `120` seconds): wall-clock limit for this cell.
- `restart` (default `false`): kill and restart the kernel. This DESTROYS every
  definition and every value in the namespace. Only use it when the kernel is
  reported dead, or when you deliberately want a clean slate.
- `verbosity`: `quiet`, `normal`, or `verbose` for the operator's terminal view
  of this one call. It changes nothing about what you receive.

Every result ends with a `NAMESPACE` line listing the callables that are live in
the kernel right now, and a `DEFINED` line for anything this cell added. That
listing is re-derived from the kernel itself on every call, so it is accurate
even when the conversation that defined a function is no longer in your context.
Read it. It is the record of what you have already built.

Output behavior:
- stdout, stderr, `repr` results, and tracebacks all come back. A cell that
  raises returns the real traceback, not a summary.
- Images produced by matplotlib and friends are written to files under
  `.js/kernel/` and reported as `IMAGE <path>`.
- The whole result is capped by `limits.max_tool_result_bytes` with a visible
  truncation marker. Do not shrink output by hand; let the cap do it.

Timeouts do not destroy your work. A cell that exceeds `timeout` is interrupted
with a `KeyboardInterrupt`, exactly like Ctrl-C in a notebook. The cell stops;
the namespace and everything in it survive. You get an `INTERRUPTED` line and
can carry straight on.

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
