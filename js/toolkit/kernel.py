"""A persistent IPython kernel as a tool, and nothing else.

This is the other side of js from the curated tool surface. An agent given one
stateful Python REPL plus a shell can build whatever else it needs mid-session:
define `parse_log` in turn 3, call it in turn 20. Two properties make that work
rather than being `exec` with extra steps.

STATE SURVIVES. One kernel per ToolContext, held for the life of the session.
A cell that hangs is interrupted with SIGINT — exactly what Ctrl-C does in a
notebook — never restarted. A runaway loop must not cost the agent the tools it
spent the session building.

THE AGENT CAN STILL SEE WHAT IT BUILT. After compaction the transcript that
defined `parse_log` may be gone while the kernel still holds the function. So
every result carries a NAMESPACE line listing the callables that are live right
now, re-derived from the kernel on every call. Never a remembered record — the
kernel is the ground truth and the listing is regenerated from it.

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
    "pyproject.toml — run `just sync` to install them. The kernel cannot run "
    "until then; every other tool is unaffected."
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
    found = {}
    for name, value in list(globals().items()):
        if name.startswith('_') or name in noise:
            continue
        if not (inspect.isfunction(value) or inspect.isclass(value)):
            continue
        if getattr(value, '__module__', None) not in (None, '__main__'):
            continue
        try:
            found[name] = name + str(inspect.signature(value))
        except (ValueError, TypeError):
            found[name] = name + '(...)'
    return json.dumps(found, sort_keys=True)
print('__JS_NS__' + __js_probe())
del __js_probe
"""

VERBOSITY_LEVELS = ("quiet", "normal", "verbose")
DEFAULT_RENDER_MAX_LINES = 24

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


@dataclass(eq=False)   # identity hash: sessions live in the _LIVE_SESSIONS set
class KernelSession:
    """One long-lived IPython kernel plus the artifacts dir for its rich output."""

    cwd: Path
    artifacts: Path
    manager: Any = None
    client: Any = None
    executions: int = 0
    artifact_seq: int = 0
    namespace: dict[str, str] = field(default_factory=dict)
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
        # IPython's own `In[n]` counter restarts too. Letting ours run on would
        # print "kernel cell 9" beside a traceback that says "Cell In[1]".
        self.executions = 0


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


def _drain_shell(session: KernelSession, msg_id: str, deadline: float) -> None:
    """Wait for the shell reply so the next execute cannot read this cell's messages."""
    while time.monotonic() < deadline:
        try:
            reply = session.client.get_shell_msg(timeout=0.2)
        except queue.Empty:
            continue
        if reply.get("parent_header", {}).get("msg_id") == msg_id:
            return


def _collect(session: KernelSession, msg_id: str, timeout: int) -> tuple[list[dict], bool, bool]:
    """Gather every iopub message for one execution until the kernel goes idle.

    Returns (messages, timed_out, died). On timeout the kernel is INTERRUPTED,
    not restarted: the cell dies, the namespace and everything the agent built
    lives. A kernel that vanished mid-cell breaks the loop instead of blocking
    until the deadline.
    """
    out: list[dict] = []
    deadline = time.monotonic() + timeout
    timed_out = False
    died = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            session.manager.interrupt_kernel()
            grace = time.monotonic() + 10  # let the KeyboardInterrupt surface
            while time.monotonic() < grace:
                try:
                    msg = session.client.get_iopub_msg(timeout=0.3)
                except queue.Empty:
                    # Keep polling to the grace deadline. A CPU-bound C
                    # extension only checks signals periodically, so the first
                    # poll after SIGINT routinely returns nothing; breaking here
                    # collapsed the 10s window to 300ms and dropped the
                    # KeyboardInterrupt traceback the caller is waiting for.
                    if not session.alive():
                        died = True
                        break
                    continue
                if msg.get("parent_header", {}).get("msg_id") != msg_id:
                    continue
                out.append(msg)
                if (msg["header"]["msg_type"] == "status"
                        and msg["content"].get("execution_state") == "idle"):
                    break
            break
        try:
            msg = session.client.get_iopub_msg(timeout=min(0.5, max(0.05, remaining)))
        except queue.Empty:
            if not session.alive():
                died = True
                break
            continue
        if msg.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        kind = msg["header"]["msg_type"]
        if kind == "status" and msg["content"].get("execution_state") == "idle":
            break
        out.append(msg)
    if not died:
        _drain_shell(session, msg_id, time.monotonic() + 5)
    return out, timed_out, died


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
    started = time.monotonic()
    msg_id = session.client.execute(code, store_history=store_history, allow_stdin=False)
    messages, timed_out, died = _collect(session, msg_id, timeout)
    result = _render_messages(session, messages, marker)
    result.timed_out = timed_out
    result.died = died
    result.elapsed = time.monotonic() - started
    return result


def refresh_namespace(session: KernelSession) -> tuple[list[str], list[str]]:
    """Re-derive live callables FROM THE KERNEL. Returns (added, removed) names.

    The listing is never accumulated across calls. Whatever the kernel says now
    is the whole truth: a name the agent deleted stops being advertised, and a
    name it defined appears without anything having to record the definition.
    """
    probe = _NAMESPACE_PROBE % {"noise": set(_IPYTHON_NOISE)}
    result = run_cell(session, probe, timeout=20, store_history=False, marker="__JS_NS__")
    if result.timed_out or result.died or not result.marker:
        return [], []
    try:
        current = json.loads(result.marker)
    except json.JSONDecodeError:
        return [], []
    added = sorted(set(current) - set(session.namespace))
    removed = sorted(set(session.namespace) - set(current))
    session.namespace = current
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


def kernel(
    code: str = "",
    timeout: int = 120,
    restart: bool = False,
    verbosity: str = "",
    context: ToolContext | None = None,
) -> str:
    if context is None:
        return "ERROR: missing ToolContext"
    code = text_or_default(code)
    limit = int_or_default(timeout, 120, minimum=1)
    level = resolve_verbosity(context, verbosity)

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
        if not code.strip():
            return "\n".join(notes)

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
    try:
        result = run_cell(session, code, limit)
    except Exception as exc:  # noqa: BLE001
        message = f"ERROR: kernel execution failed: {type(exc).__name__}: {exc}"
        render_event(context, level, message, style="bold red")
        return message

    if result.died or not session.alive():
        message = (
            f"ERROR: the kernel died during execution (cell {session.executions}). "
            "Everything defined in this session is gone; call again with restart=true "
            "and rebuild."
        )
        render_event(context, level, message, style="bold red")
        return "\n".join([*notes, message])

    added, removed = refresh_namespace(session)

    render_execution(
        context, level=level, code=code, stdout=result.stdout, stderr=result.stderr,
        display=result.display, error=result.error, elapsed=result.elapsed,
        cell=session.executions, added=[session.namespace.get(n, n) for n in added],
        removed=removed, namespace=sorted(session.namespace), images=result.images,
        interrupted=result.timed_out,
    )

    parts: list[str] = list(notes)
    if result.timed_out:
        parts.append(
            f"INTERRUPTED after {limit}s. The cell was stopped with a KeyboardInterrupt; "
            "the namespace and everything defined in it are intact."
        )
    body = result.text()
    parts.append(body.rstrip("\n") if body.strip() else "(no output)")
    if added:
        parts.append("DEFINED " + ", ".join(session.namespace.get(name, name) for name in added))
    if removed:
        parts.append("GONE " + ", ".join(removed))
    if session.namespace:
        parts.append("NAMESPACE " + ", ".join(sorted(session.namespace)))
    for image in result.images:
        parts.append(f"IMAGE {image}")
    return cap_for_model("\n".join(parts), context)


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "kernel",
            load_description("kernel"),
            kernel,
            {
                "code": {"type": "string"},
                "timeout": {"type": "integer", "default": 120},
                "restart": {"type": "boolean", "default": False},
                "verbosity": {"type": "string", "enum": list(VERBOSITY_LEVELS)},
            },
        ),
    )
