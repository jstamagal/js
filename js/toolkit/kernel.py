"""A persistent IPython kernel as a tool, and nothing else.

This is the other side of js from the curated tool surface. An agent given one
stateful Python REPL plus a shell can build whatever else it needs mid-session:
define `parse_log` in turn 3, call it in turn 20. Two properties make that work
rather than being `exec` with extra steps.

STATE SURVIVES. One kernel per ToolContext, held for the life of the session.
A cell that hangs is interrupted with SIGINT — exactly what Ctrl-C does in a
notebook — never restarted. A runaway loop must not cost the agent the tools it
spent the session building.

A CALL NEVER INHERITS THE KERNEL'S STATE. The kernel is an external process
talked to over a socket, like an MCP server: a cell is submitted with a bounded
wait and a cell still running when that wait elapses comes back as a handle to
poll. A turn cancelled mid-cell interrupts the cell before the worker running
the tool is abandoned, so the next call never queues behind a cell nobody is
watching anymore.

THE AGENT CAN STILL SEE WHAT IT BUILT. After compaction the transcript that
defined `parse_log` may be gone while the kernel still holds the function. So
every result carries a NAMESPACE line listing the functions and classes the
session defined, re-derived from the kernel on every call. Never a remembered
record — the kernel is the ground truth and the listing is regenerated from it.

This module has no opinion about tool persistence. It does not save, load, or
version anything, and it does not import `toolbox`. The learning layer sits on
top in `toolbox.py` and depends on this; the dependency never points back.
"""

from __future__ import annotations

import atexit
import base64
import json
import queue
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..capped_process import truncation_marker
from .core import Tool, ToolContext
from .descriptions import load_description
from .sanitize import int_or_default, text_or_default

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

MISSING_DEPS = (
    "ERROR: the kernel tool needs the {missing} package{plural}, which "
    "{verb} not importable in this environment. They are declared in "
    "pyproject.toml: `just install` puts them in the `js` on PATH, `just sync` "
    "puts them in this checkout's project venv. The kernel cannot run until "
    "then; every other tool is unaffected."
)

# Names IPython puts in the user namespace itself. Reporting these as things the
# agent built would bury the two functions it actually wrote under a dozen REPL
# artifacts.
_IPYTHON_NOISE = frozenset({
    "In", "Out", "get_ipython", "exit", "quit", "open",
})

# Probes run with store_history=False so they never land in `In` and never
# become part of what the agent sees. Each one deletes its own helper.
_NAMESPACE_PROBE = """
def __js_probe():
    import inspect, json
    noise = %(noise)r
    callables = {}
    names = []
    for name, value in list(globals().items()):
        if name.startswith('_') or name in noise:
            continue
        names.append(name)
        if not (inspect.isfunction(value) or inspect.isclass(value)):
            continue
        if getattr(value, '__module__', None) not in (None, '__main__'):
            continue
        try:
            callables[name] = name + str(inspect.signature(value))
        except (ValueError, TypeError):
            callables[name] = name + '(...)'
    return json.dumps({'callables': callables, 'names': sorted(names)}, sort_keys=True)
print('__JS_NS__' + __js_probe())
del __js_probe
"""

VERBOSITY_LEVELS = ("quiet", "normal", "verbose")
DEFAULT_RENDER_MAX_LINES = 24
# How long a submitted cell is waited for before the call returns a handle.
# Overridden by the `kernel.wait_seconds` knob.
DEFAULT_WAIT_SECONDS = 5
# One read of the iopub queue, in seconds. Short enough that a poll that finds
# nothing feels immediate, long enough not to spin the CPU.
POLL_SLICE = 0.25
# After SIGINT, how long to keep reading for the KeyboardInterrupt. A CPU-bound
# C extension only checks signals between chunks, so the first polls after the
# signal routinely return nothing.
INTERRUPT_GRACE = 10.0

KERNEL_ACTIONS = ("run", "poll", "interrupt", "wait")

# Handles for finished cells are kept only so the agent can still poll output it
# has not read. A session that submits hundreds of cells should not accumulate
# hundreds of message lists, so the table holds the most recent few.
KEEP_FINISHED_HANDLES = 5

# Kernels are subprocesses. Nothing in the tool protocol runs when js exits, so
# without this every session that touched the kernel would leave a live Python
# process behind holding its zmq ports. terminal.py keeps its PTY children the
# same way.
_LIVE_SESSIONS: set[Any] = set()


@atexit.register
def _shutdown_live_kernels() -> None:
    for session in list(_LIVE_SESSIONS):
        session.shutdown()


def resolve_verbosity(context: Any, override: str = "") -> str:
    """Per-call parameter beats the config knob beats 'normal'.

    Both exist on purpose. The knob (`kernel.verbosity`) is how the operator
    sets the baseline he wants to read all session without editing tool calls;
    the parameter is how one noisy cell gets squelched, or one mystery cell
    cranked to verbose, without restarting anything.
    """
    chosen = str(override or "").strip().lower()
    if chosen in VERBOSITY_LEVELS:
        return chosen
    configured = str(getattr(context, "kernel_verbosity", "") or "").strip().lower()
    if configured in VERBOSITY_LEVELS:
        return configured
    return "normal"


def render_max_lines(context: Any) -> int:
    value = int_or_default(getattr(context, "kernel_render_max_lines", None),
                           DEFAULT_RENDER_MAX_LINES, minimum=1)
    return value


# --------------------------------------------------------------------------
# Terminal rendering
#
# Two audiences, two renderings. The MODEL gets the return value of `kernel()`:
# always complete, always the same shape, capped only by
# limits.max_tool_result_bytes. The OPERATOR gets a rich panel on stderr whose
# detail the verbosity knob controls.
#
# stderr, not stdout, so `js -p '...' | jq` keeps working while the panel still
# reaches a terminal. Verbosity deliberately does NOT reshape the model-facing
# string: a display knob that silently deleted the NAMESPACE line would break
# the one property this whole tool rests on.
# --------------------------------------------------------------------------


def _console() -> Any:
    from rich.console import Console

    return Console(file=sys.stderr, soft_wrap=False, highlight=False)


def _clip(text: str, limit: int) -> tuple[str, int]:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text, 0
    return "\n".join(lines[:limit]), len(lines) - limit


def render_execution(
    context: Any,
    *,
    level: str,
    code: str,
    stdout: str,
    stderr: str,
    display: str,
    error: str,
    elapsed: float,
    cell: int,
    added: list[str],
    removed: list[str],
    namespace: list[str],
    images: list[Path],
    interrupted: bool,
) -> None:
    if level == "quiet" and not error and not interrupted:
        return
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text

    console = _console()
    limit = render_max_lines(context)

    if level == "quiet":
        head = "INTERRUPTED" if interrupted else "ERROR"
        body, hidden = _clip(error or "cell interrupted", limit)
        console.print(Text(f"kernel[{cell}] {head}: ", style="bold red") + Text(body))
        if hidden:
            console.print(Text(f"  ... {hidden} more lines (full text went to the model)",
                               style="dim"))
        return

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, style="bold cyan")
    grid.add_column(overflow="fold")

    shown_code, code_hidden = _clip(code.strip(), limit)
    grid.add_row("code", Syntax(shown_code, "python", theme="ansi_dark",
                                word_wrap=True, background_color="default"))
    if code_hidden:
        grid.add_row("", Text(f"... {code_hidden} more lines of code", style="dim"))

    if level == "verbose":
        sections = (("stdout", stdout, ""), ("stderr", stderr, "yellow"),
                    ("display", display, "magenta"))
    else:
        merged = "".join(part for part in (stdout, stderr, display) if part)
        sections = (("out", merged, ""),)

    for label, body, style in sections:
        if not body.strip():
            continue
        shown, hidden = _clip(body.rstrip("\n"), limit)
        grid.add_row(label, Text(shown, style=style or None))
        if hidden:
            grid.add_row("", Text(f"... {hidden} more lines (full text went to the model)",
                                  style="dim"))

    if error:
        shown, hidden = _clip(error.rstrip("\n"), limit)
        grid.add_row("error", Text(shown, style="red"))
        if hidden:
            grid.add_row("", Text(f"... {hidden} more lines (full text went to the model)",
                                  style="dim"))
    if interrupted:
        grid.add_row("stopped", Text("SIGINT sent; namespace intact", style="bold yellow"))
    if added:
        grid.add_row("defined", Text(", ".join(added), style="green"))
    if removed:
        grid.add_row("gone", Text(", ".join(removed), style="red"))
    grid.add_row("namespace", Text(", ".join(namespace) if namespace else "(none)"))
    for image in images:
        grid.add_row("image", Text(str(image), style="blue"))

    console.print(Panel(grid, title=f"kernel cell {cell}",
                        subtitle=f"{elapsed:.2f}s", title_align="left",
                        subtitle_align="right", border_style="cyan"))


def render_event(context: Any, level: str, message: str, *, style: str = "cyan",
                 verbose_only: bool = False) -> None:
    """Lifecycle/activity note: kernel started, restarted, died, tool saved.

    Multi-line messages print one line per line. Folding them onto one line with
    separators is how a five-tool toolbox listing became an unreadable ribbon.
    """
    if level == "quiet":
        return
    if verbose_only and level != "verbose":
        return
    from rich.text import Text

    console = _console()
    lines = message.splitlines() or [""]
    shown, hidden = _clip("\n".join(lines), render_max_lines(context))
    for line in shown.splitlines():
        console.print(Text("· ", style="dim") + Text(line, style=style))
    if hidden:
        console.print(Text(f"  ... {hidden} more lines", style="dim"))


# --------------------------------------------------------------------------
# Kernel session
# --------------------------------------------------------------------------


@dataclass
class CellHandle:
    """One submitted cell and the iopub messages the kernel produced for it.

    A handle outlives the call that made it. Output keeps arriving into
    `messages` while the agent is off doing something else, and a poll delivers
    whatever arrived since the last one.
    """

    id: str
    msg_id: str
    code: str
    started: float
    messages: list[dict] = field(default_factory=list)
    delivered: int = 0
    finished: bool = False
    died: bool = False
    timed_out: bool = False
    elapsed: float = 0.0

    def first_line(self) -> str:
        for line in self.code.strip().splitlines():
            if line.strip():
                return line.strip()[:80]
        return "(no code)"

    def age(self) -> float:
        return self.elapsed if self.finished else time.monotonic() - self.started


@dataclass(eq=False)   # identity hash: sessions live in the _LIVE_SESSIONS set
class KernelSession:
    """One long-lived IPython kernel plus the artifacts dir for its rich output."""

    cwd: Path
    artifacts: Path
    manager: Any = None
    client: Any = None
    executions: int = 0
    artifact_seq: int = 0
    sequence: int = 0
    handles: dict[str, CellHandle] = field(default_factory=dict)
    current: str = ""      # the handle the kernel is executing, "" when idle
    last: str = ""         # the most recent handle, for poll/interrupt/wait
    namespace: dict[str, str] = field(default_factory=dict)
    visible_names: set[str] = field(default_factory=set)
    log_handle: Any = None

    @property
    def log_path(self) -> Path:
        return self.artifacts / "kernel.log"

    def start(self) -> None:
        from jupyter_client.manager import KernelManager

        self.artifacts.mkdir(parents=True, exist_ok=True)
        # The kernel process writes its own chatter to stderr — the "running over
        # TCP without encryption" banner on every single start, among others.
        # Inherited, that lands on the operator's terminal ahead of the render
        # and buries it. It goes to a file instead: still there when a kernel
        # fails to boot, never on screen.
        self.log_handle = self.log_path.open("ab")
        self.manager = KernelManager(kernel_name="python3")
        self.manager.start_kernel(cwd=str(self.cwd), stdout=self.log_handle,
                                  stderr=self.log_handle)
        self.client = self.manager.blocking_client()
        self.client.start_channels()
        self.client.wait_for_ready(timeout=60)
        _LIVE_SESSIONS.add(self)

    def alive(self) -> bool:
        return self.manager is not None and self.manager.is_alive()

    def shutdown(self) -> None:
        try:
            if self.client is not None:
                self.client.stop_channels()
            if self.manager is not None:
                self.manager.shutdown_kernel(now=True)
        except Exception:  # noqa: BLE001 - teardown must never raise into a tool result
            pass
        if self.log_handle is not None:
            try:
                self.log_handle.close()
            except OSError:
                pass
            self.log_handle = None
        self.manager = None
        self.client = None
        _LIVE_SESSIONS.discard(self)

    def restart(self) -> None:
        if self.manager is None:
            self.start()
            return
        self.manager.restart_kernel(now=True)
        self.client = self.manager.blocking_client()
        self.client.start_channels()
        self.client.wait_for_ready(timeout=60)
        self.namespace = {}
        self.visible_names = set()
        self.handles = {}
        self.current = ""
        self.last = ""
        # IPython's own `In[n]` counter restarts too. Letting ours run on would
        # print "kernel cell 9" beside a traceback that says "Cell In[1]".
        self.executions = 0

    def submit(self, code: str, *, store_history: bool = True, label: str = "") -> CellHandle:
        """Send one cell and return its handle. Does not wait for the cell."""
        msg_id = self.client.execute(code, store_history=store_history, allow_stdin=False)
        self.sequence += 1
        handle = CellHandle(id=label or f"p{self.sequence}", msg_id=msg_id, code=code,
                            started=time.monotonic())
        self.handles[handle.id] = handle
        self.current = handle.id
        self.last = handle.id
        return handle

    def record(self, msg: dict) -> None:
        """File one iopub message under the cell that produced it."""
        msg_id = (msg.get("parent_header") or {}).get("msg_id")
        for handle in self.handles.values():
            if handle.msg_id != msg_id:
                continue
            if handle.finished:
                return
            handle.messages.append(msg)
            if (msg["header"]["msg_type"] == "status"
                    and msg["content"].get("execution_state") == "idle"):
                handle.finished = True
                handle.elapsed = time.monotonic() - handle.started
                if self.current == handle.id:
                    self.current = ""
            return

    def interrupt(self) -> None:
        """SIGINT the executing cell. Signals the process, so any thread may call it."""
        if self.manager is not None:
            self.manager.interrupt_kernel()

    def forget(self, handle: CellHandle) -> None:
        """Drop a handle whose whole result was already delivered."""
        self.handles.pop(handle.id, None)
        if self.current == handle.id:
            self.current = ""

    def prune(self) -> None:
        """Keep the handle table proportional to the live session, not its history."""
        finished = sorted((h for h in self.handles.values() if h.finished),
                          key=lambda h: h.started)
        for handle in finished[:-KEEP_FINISHED_HANDLES]:
            self.forget(handle)


def missing_dependencies() -> str:
    """'' when the kernel can run, else the ERROR string naming what is absent."""
    import importlib.util

    missing = [name for name in ("jupyter_client", "ipykernel")
               if importlib.util.find_spec(name) is None]
    if not missing:
        return ""
    return MISSING_DEPS.format(
        missing=" and ".join(missing),
        plural="" if len(missing) == 1 else "s",
        verb="is" if len(missing) == 1 else "are",
    )


def get_session(context: Any) -> tuple[KernelSession | None, str, bool]:
    """(session, error, started_now). Never raises; a failure comes back as text."""
    problem = missing_dependencies()
    if problem:
        return None, problem, False
    session = getattr(context, "kernel_session", None)
    if session is not None and session.alive():
        return session, "", False
    artifacts = Path(context.cwd) / ".js" / "kernel"
    session = KernelSession(cwd=Path(context.cwd), artifacts=artifacts)
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001 - a dead start is a tool result, not a crash
        return None, f"ERROR: could not start the IPython kernel: {type(exc).__name__}: {exc}", False
    context.kernel_session = session
    return session, "", True


# --------------------------------------------------------------------------
# Message plumbing
# --------------------------------------------------------------------------


def _drain_shell(session: KernelSession, deadline: float) -> None:
    """Read the shell replies nothing else is waiting for.

    Executing a cell leaves a shell reply queued behind the iopub messages. Left
    there, it is the next call's problem; draining it keeps the sockets empty.
    """
    while time.monotonic() < deadline:
        try:
            session.client.get_shell_msg(timeout=min(POLL_SLICE, deadline - time.monotonic()))
        except queue.Empty:
            return


def collect_until(session: KernelSession, handle: CellHandle, deadline: float) -> None:
    """Read iopub into `handle` until the cell finishes, the kernel dies, or the deadline.

    One quiet slice is not the end of a cell: a CPU-bound C extension only
    checks signals periodically, so a poll that comes back empty keeps polling
    to the deadline instead of treating silence as an answer.
    """
    while not handle.finished:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            msg = session.client.get_iopub_msg(timeout=min(POLL_SLICE, max(0.01, remaining)))
        except queue.Empty:
            if not session.alive():
                handle.died = True
                return
            continue
        session.record(msg)


def pump(session: KernelSession, deadline: float) -> None:
    """File every iopub message that arrives before the deadline, for whoever it belongs to."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            msg = session.client.get_iopub_msg(timeout=min(POLL_SLICE, max(0.01, remaining)))
        except queue.Empty:
            if not session.alive():
                return
            continue
        session.record(msg)


def interrupt_and_collect(session: KernelSession, handle: CellHandle,
                          grace: float = INTERRUPT_GRACE) -> None:
    """SIGINT the cell, then keep reading until it reports idle."""
    session.interrupt()
    collect_until(session, handle, time.monotonic() + grace)


def busy_handle(session: KernelSession) -> CellHandle | None:
    """The cell the kernel is still executing, if any."""
    current = getattr(session, "current", "")
    handle = getattr(session, "handles", {}).get(current) if current else None
    return handle if handle is not None and not handle.finished else None


def pick_handle(session: KernelSession, target: str) -> CellHandle | None:
    """The handle a poll/interrupt/wait names, defaulting to the live one."""
    handles = getattr(session, "handles", {})
    if target:
        return handles.get(target)
    live = busy_handle(session)
    if live is not None:
        return live
    last = getattr(session, "last", "")
    return handles.get(last) if last else None


def interrupt_inflight(context: Any) -> bool:
    """SIGINT a cell whose turn was cancelled out from under the tool.

    The runtime cannot cancel the worker thread running a tool, so a `kernel`
    call abandoned by a cancelled turn would leave its cell executing behind the
    next call. The runtime calls this before it drains that worker; the signal
    makes the cell stop so the kernel is idle for what comes next.
    """
    session = getattr(context, "kernel_session", None)
    if session is None or not session.alive():
        return False
    handle = busy_handle(session)
    if handle is None:
        return False
    session.interrupt()
    return True


@dataclass
class CellOutput:
    stdout: str = ""
    stderr: str = ""
    display: str = ""
    error: str = ""
    marker: str = ""
    images: list[Path] = field(default_factory=list)
    timed_out: bool = False
    died: bool = False
    elapsed: float = 0.0

    def text(self) -> str:
        parts = [self.stdout, self.stderr, self.display, self.error]
        return "".join(part for part in parts if part)


def _save_image(session: KernelSession, mime: str, payload: str) -> Path | None:
    ext = {"image/png": ".png", "image/jpeg": ".jpg",
           "image/gif": ".gif", "image/webp": ".webp"}.get(mime)
    if ext is None:
        return None
    session.artifact_seq += 1
    target = session.artifacts / f"cell{session.executions:04d}-{session.artifact_seq:02d}{ext}"
    try:
        session.artifacts.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(payload))
    except (OSError, ValueError):
        return None
    return target


def _render_messages(session: KernelSession, messages: list[dict], marker: str) -> CellOutput:
    """Split kernel output by stream, pull out any probe marker, save images."""
    result = CellOutput()
    for msg in messages:
        kind = msg["header"]["msg_type"]
        content = msg["content"]
        if kind == "stream":
            text = content.get("text", "")
            if marker:
                at = text.find(marker)
                if at != -1:
                    end = text.find("\n", at)
                    result.marker = text[at + len(marker):end if end != -1 else None]
                    text = text[:at] + (text[end + 1:] if end != -1 else "")
            if not text:
                continue
            if content.get("name") == "stderr":
                result.stderr += text
            else:
                result.stdout += text
        elif kind in ("execute_result", "display_data"):
            data = content.get("data", {})
            for mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                if mime not in data:
                    continue
                saved = _save_image(session, mime, data[mime])
                if saved is not None:
                    result.images.append(saved)
                    result.display += f"[{mime} saved to {saved}]\n"
                break
            else:
                plain = data.get("text/plain")
                if plain:
                    result.display += plain if plain.endswith("\n") else plain + "\n"
        elif kind == "error":
            trace = "\n".join(_ANSI.sub("", line) for line in content.get("traceback", []))
            result.error += (trace or
                             f"{content.get('ename', 'Error')}: {content.get('evalue', '')}") + "\n"
    return result


def run_cell(session: KernelSession, code: str, timeout: int, *,
             store_history: bool = True, marker: str = "") -> CellOutput:
    """Submit a cell and block for the whole answer: the internal probe path.

    Model-facing calls go through `submit_cell`, which hands back a handle when
    the wait elapses. This one is for the round trips the tool makes on its own
    account — the namespace probe, the toolbox probes — where there is nothing
    for the agent to poll and half an answer is no answer. On timeout the cell
    is interrupted, never restarted.
    """
    started = time.monotonic()
    handle = session.submit(code, store_history=store_history)
    collect_until(session, handle, started + timeout)
    if not handle.finished and not handle.died:
        handle.timed_out = True
        interrupt_and_collect(session, handle)
    result = _render_messages(session, handle.messages, marker)
    handle.delivered = len(handle.messages)
    session.forget(handle)
    if not handle.died:
        _drain_shell(session, time.monotonic() + 1)
    result.timed_out = handle.timed_out
    result.died = handle.died
    result.elapsed = time.monotonic() - started
    return result


def submit_cell(session: KernelSession, code: str, wait: float, *,
                label: str = "") -> tuple[CellHandle, CellOutput]:
    """Submit a model-facing cell and collect for `wait` seconds.

    Returns the handle and the output produced so far. The cell keeps running
    when the wait elapses; the handle is how the agent follows it.
    """
    handle = session.submit(code, label=label)
    session.prune()
    collect_until(session, handle, time.monotonic() + wait)
    output = _render_messages(session, handle.messages, "")
    handle.delivered = len(handle.messages)
    if handle.finished:
        _drain_shell(session, time.monotonic() + 1)
    return handle, output


def poll_cell(session: KernelSession, handle: CellHandle) -> CellOutput:
    """Collect whatever arrived since the last read of `handle`."""
    pump(session, time.monotonic() + POLL_SLICE)
    output = _render_messages(session, handle.messages[handle.delivered:], "")
    handle.delivered = len(handle.messages)
    if handle.finished:
        _drain_shell(session, time.monotonic() + 1)
    return output


def wait_cell(session: KernelSession, handle: CellHandle, timeout: int) -> CellOutput:
    """Block up to `timeout` for a submitted cell, interrupting it if the wait expires."""
    collect_until(session, handle, time.monotonic() + timeout)
    if not handle.finished and not handle.died:
        handle.timed_out = True
        interrupt_and_collect(session, handle)
    return poll_cell(session, handle)


def refresh_namespace(session: KernelSession) -> tuple[list[str], list[str]]:
    """Re-derive the live namespace FROM THE KERNEL. Returns (added, removed).

    The listing is never accumulated across calls. Whatever the kernel says now
    is the whole truth: a name the agent deleted stops being advertised, and a
    name it defined appears without anything having to record the definition.
    `session.namespace` holds the callables and their signatures; `added` and
    `removed` cover every visible name, so an import or a plain value is
    reported as defined even though it is not callable.
    """
    probe = _NAMESPACE_PROBE % {"noise": set(_IPYTHON_NOISE)}
    result = run_cell(session, probe, timeout=20, store_history=False, marker="__JS_NS__")
    if result.timed_out or result.died or not result.marker:
        return [], []
    try:
        payload = json.loads(result.marker)
    except json.JSONDecodeError:
        return [], []
    if not isinstance(payload, dict):
        return [], []
    callables = payload.get("callables")
    names = payload.get("names")
    if not isinstance(callables, dict) or not isinstance(names, list):
        return [], []
    visible = set(names)
    added = sorted(visible - session.visible_names)
    removed = sorted(session.visible_names - visible)
    session.namespace = callables
    session.visible_names = visible
    return added, removed


def cap_for_model(text: str, context: Any) -> str:
    """Cap the model-facing string with js's own knob and a visible marker."""
    budget = int_or_default(getattr(context, "max_tool_result_bytes", None), 256 * 1024, minimum=1)
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    marker = truncation_marker(budget, "limits.max_tool_result_bytes")
    keep = max(0, budget - len(marker.encode("utf-8")) - 1)
    return encoded[:keep].decode("utf-8", errors="ignore") + "\n" + marker


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------


def wait_seconds(context: Any) -> float:
    """How long a submitted cell is waited for before the call returns a handle."""
    return float(int_or_default(getattr(context, "kernel_wait_seconds", None),
                                DEFAULT_WAIT_SECONDS, minimum=1))


def _output_parts(output: CellOutput) -> list[str]:
    text = output.text()
    parts = [text.rstrip("\n") if text.strip() else "(no output)"]
    parts.extend(f"IMAGE {image}" for image in output.images)
    return parts


def _finished_parts(session: KernelSession, output: CellOutput) -> list[str]:
    """The tail of a result for a cell the kernel has finished with."""
    added, removed = refresh_namespace(session)
    parts = _output_parts(output)
    if added:
        parts.append("DEFINED " + ", ".join(session.namespace.get(name, name) for name in added))
    if removed:
        parts.append("GONE " + ", ".join(removed))
    parts.append("NAMESPACE " + (", ".join(sorted(session.namespace)) or "(none)"))
    return parts


def _running_parts(handle: CellHandle, output: CellOutput) -> list[str]:
    parts = _output_parts(output)
    parts.append(f"HANDLE {handle.id} RUNNING")
    return parts


def _died(context: ToolContext, session: KernelSession, level: str, notes: list[str],
          cell: str) -> str:
    session.namespace = {}
    session.visible_names = set()
    session.handles = {}
    session.current = ""
    message = (
        f"ERROR: the kernel died during execution (cell {cell}). "
        "Everything defined in this session is gone; call again with restart=true "
        "and rebuild."
    )
    render_event(context, level, message, style="bold red")
    return "\n".join([*notes, message, "NAMESPACE (none)"])


def _busy(context: ToolContext, session: KernelSession, level: str, notes: list[str],
          live: CellHandle) -> str:
    message = (f"ERROR: the previous cell is still running ({live.first_line()}); "
               "interrupt it or wait")
    hint = (f'handle {live.id}: action="poll" for new output, action="interrupt" to stop it, '
            'action="wait" to block for it. A cell blocked in a syscall that ignores '
            "SIGINT needs restart=true")
    render_event(context, level, message, style="bold red")
    return cap_for_model("\n".join([*notes, message, hint, f"HANDLE {live.id} RUNNING"]), context)


def _report(context: ToolContext, session: KernelSession, level: str, notes: list[str],
            handle: CellHandle, output: CellOutput, *, status: str = "",
            timed_out_note: str = "") -> str:
    """Render a cell that may or may not have finished into the model-facing string."""
    if handle.died or not session.alive():
        return _died(context, session, level, notes, handle.id)
    parts: list[str] = list(notes)
    if timed_out_note:
        parts.append(timed_out_note)
    if status:
        parts.append(status)
    if handle.finished:
        parts.extend(_finished_parts(session, output))
    else:
        parts.extend(_running_parts(handle, output))
    return cap_for_model("\n".join(parts), context)


def kernel(
    code: str = "",
    timeout: int = 120,
    restart: bool = False,
    verbosity: str = "",
    action: str = "",
    handle: str = "",
    context: ToolContext | None = None,
) -> str:
    if context is None:
        return "ERROR: missing ToolContext"
    code = text_or_default(code)
    limit = int_or_default(timeout, 120, minimum=1)
    mode = text_or_default(action, "run").strip().lower() or "run"
    target = text_or_default(handle).strip()
    level = resolve_verbosity(context, verbosity)
    if mode not in KERNEL_ACTIONS:
        return f"ERROR: action must be one of {', '.join(KERNEL_ACTIONS)}"

    session, problem, started_now = get_session(context)
    if session is None:
        render_event(context, level, problem, style="bold red")
        return problem
    notes: list[str] = []
    if started_now:
        render_event(context, level, f"kernel started in {session.cwd}", verbose_only=True)
        notes.append(f"kernel started (cwd {session.cwd})")

    if restart:
        try:
            session.restart()
        except Exception as exc:  # noqa: BLE001
            message = f"ERROR: kernel restart failed: {type(exc).__name__}: {exc}"
            render_event(context, level, message, style="bold red")
            return message
        render_event(context, level, "kernel restarted — namespace cleared", style="yellow")
        notes.append("kernel restarted; the namespace is empty")
        if mode == "run" and not code.strip():
            return "\n".join([*notes, "NAMESPACE (none)"])

    if mode in ("poll", "interrupt", "wait"):
        live = pick_handle(session, target)
        if live is None:
            message = (f"ERROR: no cell {('handle ' + target) if target else 'is running'}; "
                       "nothing to poll, interrupt, or wait for")
            render_event(context, level, message, style="bold red")
            return cap_for_model("\n".join([*notes, message]), context)
        if mode == "poll":
            output = poll_cell(session, live)
            status = (f"cell {live.id} finished ({live.age():.1f}s)." if live.finished
                      else f"cell {live.id} is still running ({live.age():.1f}s).")
            return _report(context, session, level, notes, live, output, status=status)
        if mode == "interrupt":
            already_finished = live.finished
            if not already_finished:
                interrupt_and_collect(session, live)
            output = poll_cell(session, live)
            if already_finished:
                status = f"cell {live.id} had already finished ({live.age():.1f}s)."
            elif live.finished:
                status = f"interrupt sent to cell {live.id}; it stopped ({live.age():.1f}s)."
            else:
                status = (f"interrupt sent to cell {live.id}; it is still running "
                          f"({live.age():.1f}s).")
            return _report(context, session, level, notes, live, output, status=status)
        output = wait_cell(session, live, limit)
        status = (f"cell {live.id} finished ({live.age():.1f}s)." if live.finished
                  else f"cell {live.id} is still running ({live.age():.1f}s).")
        return _report(
            context, session, level, notes, live, output, status=status,
            timed_out_note=(
                f"INTERRUPTED after {limit}s. The cell was stopped with a KeyboardInterrupt; "
                "the namespace and everything defined in it are intact."
                if live.timed_out else ""),
        )

    live = busy_handle(session)
    if live is not None:
        return _busy(context, session, level, notes, live)

    if not code.strip():
        added, removed = refresh_namespace(session)
        live = ", ".join(sorted(session.namespace)) or "(none)"
        render_execution(
            context, level=level, code="# namespace query", stdout="", stderr="",
            display="", error="", elapsed=0.0, cell=session.executions,
            added=added, removed=removed, namespace=sorted(session.namespace),
            images=[], interrupted=False,
        )
        return "\n".join([*notes, f"NAMESPACE {live}"])

    session.executions += 1
    window = min(wait_seconds(context), limit)
    try:
        live, output = submit_cell(session, code, window, label=str(session.executions))
    except Exception as exc:  # noqa: BLE001
        message = f"ERROR: kernel execution failed: {type(exc).__name__}: {exc}"
        render_event(context, level, message, style="bold red")
        return message

    if live.died or not session.alive():
        return _died(context, session, level, notes, live.id)

    if live.finished:
        added, removed = refresh_namespace(session)
    else:
        added, removed = [], []
    render_execution(
        context, level=level, code=code, stdout=output.stdout, stderr=output.stderr,
        display=output.display, error=output.error, elapsed=live.age(),
        cell=session.executions, added=[session.namespace.get(n, n) for n in added],
        removed=removed, namespace=sorted(session.namespace), images=output.images,
        interrupted=live.timed_out,
    )

    if live.finished:
        parts: list[str] = list(notes)
        if live.timed_out:
            parts.append(
                f"INTERRUPTED after {limit}s. The cell was stopped with a KeyboardInterrupt; "
                "the namespace and everything defined in it are intact."
            )
        parts.extend(_output_parts(output))
        # Images are reported before the footer so every result really does end with
        # the NAMESPACE line the description promises.
        if added:
            parts.append("DEFINED " + ", ".join(session.namespace.get(n, n) for n in added))
        if removed:
            parts.append("GONE " + ", ".join(removed))
        parts.append("NAMESPACE " + (", ".join(sorted(session.namespace)) or "(none)"))
        return cap_for_model("\n".join(parts), context)

    parts = [
        *notes,
        f"cell {session.executions} is still running after {window:.1f}s (handle {live.id}). "
        f'Poll it with action="poll", handle="{live.id}"; interrupt it with action="interrupt".',
        *_output_parts(output),
        f"HANDLE {live.id} RUNNING",
    ]
    return cap_for_model("\n".join(parts), context)


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "kernel",
            load_description("kernel"),
            kernel,
            {
                "code": {"type": "string"},
                "action": {"type": "string", "enum": list(KERNEL_ACTIONS), "default": "run"},
                "handle": {"type": "string"},
                "timeout": {"type": "integer", "default": 120},
                "restart": {"type": "boolean", "default": False},
                "verbosity": {"type": "string", "enum": list(VERBOSITY_LEVELS)},
            },
        ),
    )
