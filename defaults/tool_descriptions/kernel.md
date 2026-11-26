Run Python in a stateful Jupyter (IPython) kernel. State persists across
calls: this is one long-lived process for the whole session, not a fresh
program per call. Every function, class, import, connection and value a cell
defines is still there on the next call and every call after it, so define a
thing once and use its name from then on.

Parameters:
- `code`: the Python to run. Empty `code` just reports the namespace.
- `action`: `run` (default) submits `code`; `poll`, `wait` and `interrupt` act
  on a cell that is still running.
- `handle`: the cell a `poll`, `wait` or `interrupt` acts on. Defaults to the
  running cell, or the last one submitted.
- `timeout` (default `120` seconds): the longest one call blocks.
- `restart`: kill the kernel and start a fresh one. Every definition and value
  is destroyed. Use it when the result says the kernel died, or when a cell
  will not stop on `interrupt`.
- `verbosity`: `quiet`, `normal` or `verbose`, for the operator's terminal view.

A cell still running after a few seconds comes back as `HANDLE <id> RUNNING`.
`action="poll"` returns what it has printed since the last read,
`action="wait"` blocks for the rest, and `action="interrupt"` stops it. A
`wait` that runs out its `timeout` interrupts the cell too. An interrupt is
Ctrl-C in a notebook: the cell gets a `KeyboardInterrupt` and the namespace
survives. New `code` submitted while a cell runs is refused, so poll, wait or
interrupt first.

If the kernel process dies (a segfault, `os._exit`, the OOM killer), the result
says so and names the cell; everything defined is gone. A kernel that died
between calls is replaced by the next call, which opens with `kernel started`
and says the previous kernel died.

Every finished cell ends with `NAMESPACE`, the functions and classes the
session has defined, and `DEFINED`, what this cell added. `NAMESPACE` is read
from the live kernel, so it is the record of what you have built even when the
turns that built it are gone from your context. Name your functions: a named
function is listed there and stays callable, a pasted block is neither.

Output: stdout, stderr, the last expression's `repr` and full tracebacks come
back. Figures are written under `~/.js/state/kernel/` and reported as
`IMAGE <path>`. A large result is previewed within `TOOL_RESULT_MAX` and
spills to a file above `TOOL_RESULT_INLINE_MAX`; the preview cuts printed
output first, so the traceback, `IMAGE` lines and the closing `HANDLE` or
`NAMESPACE` line still arrive. Print what you want and let the cap trim it.

Async: a cell runs on a live event loop. Use top-level `await` and call async
methods directly. `asyncio.run(...)` and `loop.run_until_complete(...)` raise
`This event loop is already running`, and anything that blocks the cell on its
own loop, such as `run_coroutine_threadsafe(coro, loop).result()` or a
library's sync wrapper around an async client, hangs however fast the
coroutine is. Build an async client inside the function that uses it and
`await client.aclose()` in a `finally`; one left open prints `Unclosed client
session` into later results.

Calling js tools from a cell: the namespace holds `tools`, and
`tools.<name>(...)` calls a js tool and returns its text as a Python value, so
a loop over files or a filter over search hits runs in the cell and only what
you print comes back. `<name>` is the tool's short js name, such as
`tools.read` or `tools.fs_search`, whatever longer or prefixed names your tool
schema shows. `tools.names()` lists the names a cell can use.

{{#if read}}
    src = tools.read("setup.py", show_line_numbers=False)
{{/if}}
{{#if fs_search}}
    hits = tools.fs_search(pattern="TODO", output_mode="files_with_matches")
{{/if}}

- Arguments are the tool's parameters, by keyword or in schema order. The
  value is the whole text a direct call returns, uncapped.
- A refusal or failure raises `tools.ToolError` carrying the `ERROR` text.
- A call runs as a direct call does: same argument checks, path limits,
  read-before-write rule and hooks.
{{#if patch}}
  A file read in a cell counts as read for a later edit of it.
{{/if}}
- Relative paths resolve against js's working directory, not the cell's
  `os.getcwd()`; after an `os.chdir`, pass absolute paths.
- `kernel`, the toolbox tool, subagent tools and MCP tools are not in
  `tools`; call them directly.
- Interrupting a cell does not stop the tool call it is waiting on; that call
  runs to its end.

Packages: cells run in the interpreter that runs js. To add a package, install
into that interpreter: `uv pip install --python <sys.executable> <package>`.
It stays until the next `just sync`.

{{#if shell}}
Use the shell tool for what is really a command: builds, tests, git, package
managers. A shell call is a fresh process each time; this one remembers.
{{/if}}
{{#if toolbox}}
The kernel ends with the session; the toolbox tool is what lasts. Call it with
`action=load` at the start to bring saved tools into this namespace; none of
them are here until it has run. When a function you wrote here works, save it
then: the toolbox tool with `action=save name=<fn>`.
{{/if}}
{{#unless toolbox}}
The kernel ends with the session. Write what must outlive it to a file.
{{/unless}}
Under `-C DIR` the kernel runs in the same jail as the shell tool.
