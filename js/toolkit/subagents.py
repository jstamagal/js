"""Subagent delegation (task, named-agent tools, the subagent gate)."""

from __future__ import annotations

import asyncio
import fcntl
import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .. import (
    login_defaults,
    output,
    paths,
    session_store,
    settings as _settings,
    snippets,
    turn_settings as _turn_settings,
)
from ..events import RefusableOnly
from ..text_bytes import cap_text
from . import task_jobs
from .core import Tool, ToolContext
from .descriptions import load_description, load_parameter_description
from .presentation import Presentation
from .sanitize import int_or_default, text_or_default


@dataclass(frozen=True)
class SubagentAssembly:
    """Services and output policy shared by a parent's delegated turns."""

    config_factory: Any = None
    registry_factory: Any = None
    worker: Any = None
    turn: Any = None
    write_latest: Any = None
    suppress_output: bool = True


# How the running subagent was launched: "task" (the task tool), "agent" (a
# named-agent tool) or "background" (task with background=true). Set in the
# coroutine each fan-out schedules, so it is the launching call's.
_LAUNCH_KIND: ContextVar[str] = ContextVar("js_subagent_launch_kind", default="task")

# Opens a window for a subagent this context launches: called with the
# subagent_start fields, it returns the window (`subagent_windows.SubagentWindow`)
# or None when none is free. The REPL screen sets it; None means no windows.
window_opener: ContextVar[Any] = ContextVar("js_subagent_window_opener", default=None)


class _Waiter:
    __slots__ = ("loop", "future", "limit", "granted")

    def __init__(self, loop: asyncio.AbstractEventLoop, limit: int) -> None:
        self.loop = loop
        self.future: asyncio.Future[None] = loop.create_future()
        self.limit = limit
        self.granted = False


class SubagentGate:
    """Process-wide count of running subagents, held to SUBAGENT_WORKERS_MAX.

    Subagents run on more than one event loop (the REPL's, a `-p` run's, a
    background job's private loop), so the count sits behind a thread lock and
    a waiter is woken on its own loop. Waiters are served in arrival order.
    Each acquire carries the limit its caller's settings give."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = 0
        self._waiters: deque[_Waiter] = deque()

    @property
    def running(self) -> int:
        return self._running

    async def acquire(self, limit: int) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if not self._waiters and self._running < limit:
                self._running += 1
                return
            waiter = _Waiter(loop, limit)
            self._waiters.append(waiter)
        try:
            await waiter.future
        except BaseException:
            with self._lock:
                if waiter.granted:
                    self._release_locked()
                elif waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._wake_locked()
            raise

    def release(self) -> None:
        with self._lock:
            self._release_locked()

    def _release_locked(self) -> None:
        self._running -= 1
        self._wake_locked()

    def _wake_locked(self) -> None:
        while self._waiters and self._running < self._waiters[0].limit:
            waiter = self._waiters.popleft()
            waiter.granted = True
            self._running += 1
            try:
                waiter.loop.call_soon_threadsafe(_grant, waiter.future)
            except RuntimeError:  # its loop is closed: nobody is waiting
                waiter.granted = False
                self._running -= 1


def _grant(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


_GATE = SubagentGate()


@dataclass
class _Slot:
    """The gate slot one running subagent holds. While it waits on its own
    subagents it lends the slot to them, so a parent blocked on its children
    never holds a place they need."""

    limit: int
    held: bool = True
    lent: int = 0
    # False once its subagent run is over; a later turn in its window runs
    # outside the gate.
    active: bool = True


# The slot of the subagent this coroutine runs in; None in the main agent.
_SLOT: ContextVar[_Slot | None] = ContextVar("js_subagent_slot", default=None)


@asynccontextmanager
async def _lend_slot():
    """Give up this subagent's slot while it waits on subagents it started."""
    slot = _SLOT.get()
    if slot is None or not slot.active:
        yield
        return
    slot.lent += 1
    if slot.held:
        slot.held = False
        _GATE.release()
    try:
        yield
    except Exception:
        await _take_back(slot)
        raise
    except BaseException:
        # Cancelled: the subagent is ending, it needs no slot to end.
        slot.lent -= 1
        raise
    else:
        await _take_back(slot)


async def _take_back(slot: _Slot) -> None:
    slot.lent -= 1
    if slot.lent == 0 and not slot.held and slot.active:
        await _GATE.acquire(slot.limit)
        if slot.lent or slot.held or not slot.active:
            _GATE.release()
        else:
            slot.held = True


def _route(model: str, provider: str | None) -> str:
    """``provider/model`` as a route names it; the model alone when it already
    carries the provider or there is none."""
    if not provider or model.startswith(f"{provider}/"):
        return model
    return f"{provider}/{model}"


def _tool_count(registry: Any) -> int:
    try:
        return len(registry.openai_specs())
    except Exception:  # noqa: BLE001 - a registry stub has no specs
        return 0


def _launch_fields(idx: int, total: int, agent: str, cfg: Any, registry: Any) -> dict[str, Any]:
    """What the subagent_start event says of a child about to run: its own
    model, the effort its requests send, its tools and its session."""
    from ..runtime import _with_login_defaults

    effort = _with_login_defaults(cfg, None, None, None).reasoning_effort
    session = Path(cfg.session_file)
    saved = session != Path(os.devnull)
    return {
        "agent": agent,
        "kind": _LAUNCH_KIND.get(),
        "index": idx,
        "total": total,
        "model": cfg.model,
        "provider": cfg.provider_id,
        "route": _route(cfg.model, cfg.provider_id),
        "effort": effort or "",
        "tools": _tool_count(registry),
        "session": session.stem if saved else "",
        "session_file": str(session) if saved else "",
    }


def _task_text(item: Any) -> str:
    return str(item).strip()


def _child_context(parent: ToolContext, registry: Any, agent: str) -> ToolContext:
    # A subagent gets its own kernel (kernel_session stays None) but the
    # parent's per-turn settings, kernel verbosity among them.
    child = ToolContext(cwd=parent.cwd, model=parent.model, **_turn_settings.inherit(parent))
    child.subagent_assembly = parent.subagent_assembly
    child.tool_registry = registry
    child.agent_id = agent
    child.task_depth = parent.task_depth + 1
    return child


def _session_file_for_id(agent_dir: Path, sessions_dir: Path, session_id: str, *, write_latest=None) -> Path:
    raw = Path(session_id).expanduser()
    if raw.is_absolute():
        path = raw
    else:
        path = sessions_dir / (raw if raw.suffix else raw.with_suffix(".jsonl"))
        if path.suffix != ".jsonl":
            raise ValueError(f"session_id must resolve to a .jsonl file: {session_id}")
    resolved = path.resolve(strict=False)
    if resolved.suffix != ".jsonl" or not resolved.is_relative_to(sessions_dir.resolve(strict=False)):
        raise ValueError(f"session path must be a .jsonl file inside {sessions_dir}: {session_id}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    (session_store.write_latest if write_latest is None else write_latest)(agent_dir, path)
    return path


def _reserve_worker_session(agent_dir: Path, sessions_dir: Path, *, write_latest=None) -> Path:
    session_file = session_store.reserve(sessions_dir, session_store.task_name)
    (session_store.write_latest if write_latest is None else write_latest)(agent_dir, session_file)
    return session_file


@contextmanager
def _worker_session(session_file: Path):
    """Hold one worker's conversation through replay, execution and persistence."""
    if session_file == Path(os.devnull):
        yield
        return
    lock_path = session_file.resolve().with_suffix(".task.lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"worker session is already running: {session_file}") from exc
        yield


def _agent_cfg(parent_cfg: Any, agent: str, session_id: str | None, *, write_latest=None) -> Any:
    from .. import persona as P
    from ..mcp_config import resolve as resolve_mcp

    # The per-agent data dir holds that agent's latest.json and input history.
    # A subagent run is filed in the folder named after its parent session; a
    # parent that is not saved has children that are not saved either. MCP
    # policy is an agent authorization boundary, so resolve it for the child
    # from the merged settings instead of copying the parent's already-resolved
    # policy.
    agents_root = parent_cfg.agent_dir.parent
    agent_dir = agents_root / agent
    parent_file = Path(parent_cfg.session_file)
    if parent_file == Path(os.devnull):
        sessions_dir = parent_cfg.sessions_dir
        session_file = parent_file
    else:
        sessions_dir = session_store.subagent_folder(parent_file)
        session_file = (_session_file_for_id(agent_dir, sessions_dir, session_id, write_latest=write_latest) if session_id
                        else _reserve_worker_session(agent_dir, sessions_dir, write_latest=write_latest))
    return replace(
        parent_cfg,
        agent_id=agent,
        agent_dir=agent_dir,
        sessions_dir=sessions_dir,
        session_file=session_file,
        prompts_dir=paths.agent_dirs(agent, P.config_roots(parent_cfg)),
        mcp=resolve_mcp(parent_cfg.settings, agent),
    )


async def _run_one_task_async(
    idx: int,
    total: int,
    item: Any,
    parent_context: ToolContext,
    parent_cfg: Any,
    full_registry: Any,
    agent_id: str,
    global_session_id: str | None,
    model: str = "",
    reasoning: str = "",
) -> str:
    from .. import agent_messages, persona as P, run_assembly
    from ..conversation import Conversation
    from ..runtime import Telemetry

    assembly = parent_context.subagent_assembly or SubagentAssembly()
    prompt = _task_text(item)
    if not prompt:
        return "ERROR task is empty"

    agent = agent_id
    task_session_id = global_session_id
    # Validate before reserving a session or changing its latest pointer.
    try:
        prompt_spec = run_assembly.load_spec(replace(
            parent_cfg, agent_id=agent,
            prompts_dir=paths.agent_dirs(agent, P.config_roots(parent_cfg)),
        ))
    except run_assembly.RunNotSettled as exc:
        return f"ERROR could not load agent {agent!r}: {exc}"

    cfg = _agent_cfg(parent_cfg, agent, task_session_id, write_latest=assembly.write_latest)

    # Subagent model precedence:
    #   tool-call model (main agent wins, unless lock_subagent_model) >
    #   inherit parent (if prefer_inherit) > agent.yaml primary (`model:`) >
    #   parent model as fallback.
    locked = bool(getattr(parent_cfg, "lock_subagent_model", False))
    if model and not locked:
        chosen = model
    elif getattr(parent_cfg, "prefer_inherit", False):
        chosen = parent_cfg.model
    elif getattr(prompt_spec, "model", ""):
        chosen = prompt_spec.model
    else:
        chosen = parent_cfg.model
    if chosen and chosen != cfg.model:
        cfg = run_assembly.route(cfg, chosen)
    try:
        # The child's tools and persona follow its own model, not the parent's.
        prompt_spec = run_assembly.for_model(prompt_spec, cfg)
    except run_assembly.RunNotSettled as exc:
        return f"ERROR could not load agent {agent!r}: {exc}"
    # The child's own model and agent.yaml settle its login-default settings;
    # what its agent.yaml leaves out it takes from the parent's agent. An
    # effort the parent's turn ran with that its layers do not give (a resumed
    # session's) is the child's agent effort unless its agent.yaml sets one.
    # Effort precedence for a child, lowest first: its model's login
    # defaults < the agent layer (the parent agent's or inherited effort, a
    # resumed one included, replaced by the child's agent.yaml reasoning) <
    # env < flag < /set. So the operator's env, flag and /set effort beat a
    # child's agent.yaml.
    cfg = login_defaults.for_child(cfg)
    ran = getattr(parent_cfg, "reasoning_effort", None)
    if ran is not None and ran != login_defaults.for_turn(parent_cfg).reasoning_effort:
        cfg = replace(cfg, layer_agent={**(getattr(cfg, "layer_agent", None) or {}), "EFFORT": ran})
    cfg = login_defaults.for_turn(login_defaults.with_agent(cfg, prompt_spec, inherit=True))
    if getattr(cfg, "layer_jsrc", None) is None:
        # A Config with no layers resolves nothing: agent.yaml's reasoning is
        # its effort, and its max_tokens its cap when it has none.
        if prompt_spec.reasoning_effort is not None:
            cfg = replace(cfg, reasoning_effort=prompt_spec.reasoning_effort)
        if prompt_spec.max_output_tokens is not None and cfg.max_output_tokens is None:
            cfg = replace(cfg, max_output_tokens=prompt_spec.max_output_tokens)
    if reasoning:
        # The tool call's reasoning wins, as its model does.
        cfg = replace(cfg, reasoning_effort=reasoning)
    with _worker_session(Path(cfg.session_file)):
        prompt_spec = run_assembly.expand(prompt_spec, cfg)

        registry = full_registry.select(prompt_spec.tool_selectors, agent_id=agent)
        system = prompt_spec.system
        sampling = login_defaults.sampling(cfg)

        child_context = _child_context(parent_context, registry, agent)
        child_context.config = cfg
        from ..usage import session_key

        child_context.usage_chain = (session_key(Path(parent_cfg.session_file), parent_context),
                                     *parent_context.usage_chain)
        child_context.net_label = f"Subagent {idx}"
        if Path(cfg.session_file) != Path(os.devnull):
            from ..session_catalog import record_session_start

            record_session_start(cfg.session_file, cwd=child_context.cwd, agent=agent, model=cfg.model,
                                 mode="subagent", parent=parent_cfg.session_file)
        telemetry = Telemetry(debug_log=cfg.debug_log)
        chat = Conversation(cfg, system, registry=registry, context=child_context, telemetry=telemetry,
                            run_turn=assembly.turn)
        parent_hooks = parent_context.tool_call_hooks
        parent_approval = parent_context.tool_approval
        launch = _launch_fields(idx, total, agent, cfg, registry)
        opener = window_opener.get()
        window = opener(launch) if opener is not None else None
        # The window it shows in: None with no windows, 0 when none was free.
        launch["window"] = None if opener is None else (window.number if window is not None else 0)
        output.emit(output.event("subagent_start", **launch))
        if window is not None:
            # Its window shows what it streams, as window 1 shows a REPL turn.
            telemetry = chat.telemetry = window.telemetry(cfg.debug_log)
            window.running(child_context)
            with window.writing():
                output.sink_of(telemetry).emit(output.event("subagent_start", **launch))
        started = time.monotonic()
        call_stats: list[dict] = []
        outcome = "error"
        error: str | None = None
        turn_kwargs = {
            "trace_override": None if window is not None else False,
            "suppress_output": False if window is not None else assembly.suppress_output,
            "sampling": sampling,
            "event_hooks": None if parent_hooks is None else RefusableOnly(parent_hooks),
            "approve": parent_approval,
        }
        if window is not None:
            turn_kwargs["steer"] = window.steer
        if reasoning:
            # Every request resolves the effort from the layers again; the
            # tool call's reasoning is told to the turn so it is what is sent.
            turn_kwargs["reasoning_effort_override"] = reasoning
        try:
            with window.writing() if window is not None else nullcontext():
                await agent_messages.initialize(cfg, prompt_spec, chat.messages, telemetry, tool_registry=registry,
                                                tool_context=child_context, checkpoint=chat.save, **turn_kwargs)
                await chat.turn({"role": "user", "content": prompt}, call_stats=call_stats, **turn_kwargs)
            outcome = "ok"
        except snippets.SnippetError:
            raise
        except Exception as exc:  # noqa: BLE001
            error = f"ERROR {type(exc).__name__}: {exc}"
        except BaseException:
            outcome = "cancelled"
            raise
        finally:
            end = output.event(
                "subagent_end", **launch, outcome=outcome, elapsed_s=time.monotonic() - started,
                prompt_tokens=sum(int(stat.get("prompt_tokens") or 0) for stat in call_stats),
                output_tokens=sum(int(stat.get("output_tokens") or 0) for stat in call_stats),
                calls=len(call_stats))
            output.emit(end)
            _close_child_tools(child_context)
            if window is not None:
                with window.writing():
                    output.sink_of(telemetry).emit(end)

                async def resume(text: str) -> None:
                    """One turn typed in its window, on its own session."""
                    with _worker_session(Path(cfg.session_file)), window.writing():
                        try:
                            await chat.turn({"role": "user", "content": text}, **turn_kwargs)
                        finally:
                            _close_child_tools(child_context)

                # Called before the session lock is let go; a turn it starts
                # runs as its own job, after this one has returned.
                window.finished(resume)

    if error is not None:
        return error

    final = chat.answer().strip() or "(no final response)"
    # Give each child a FAIR SHARE of the aggregate budget, not the whole thing.
    # The parent's dispatch layer re-clips the joined TASK_RESULTS at
    # max_tool_result_bytes; if every child could fill that full budget, one fat
    # sibling would fill it alone and the aggregate re-clip would slice the rest
    # away — the short siblings vanish. Capping each at budget//total bounds a fat
    # sibling to its 1/N slice, so the short ones always survive the join. (A run
    # where ALL N are fat still trims the tail at the aggregate cap — inherent to a
    # fixed budget, not silent starvation.) Read the parent context: the child's
    # runtime rewrites child_context.max_tool_result_bytes mid-turn.
    budget = int(parent_context.max_tool_result_bytes or 0)
    if budget > 0 and parent_context.subagent_result_share:
        cap = budget // max(1, total)
        final = cap_text(final, cap, f"\n[truncated: TOOL_RESULT_MAX ({cap}) reached]")
    return final


def _close_child_tools(child_context: ToolContext) -> None:
    """Close a child's terminal sessions and its private kernel."""
    from .terminal import close_terminal_sessions

    close_terminal_sessions(child_context)
    if child_context.kernel_session is not None:
        child_context.kernel_session.shutdown()
        child_context.kernel_session = None


def _fan_out(indexed_items: list[tuple[int, Any]], coro_factory) -> list[str | None]:
    """Run each subagent turn concurrently, results returned in task order. Two
    ramps, chosen by whether a REPL supervisor is live:

    - Supervisor present: schedule each turn as a cancelable "subagent" job on
      the REPL's shared loop via `run_coroutine_threadsafe` (we're on a
      tool-dispatch executor thread here), then block on each result — the
      parent turn can't proceed without them anyway. This is what makes the
      subagents visible/cancelable instead of detached on a private loop.
    - No supervisor (`js -p`, bench, tests): spin one throwaway loop and gather
      the turns here.
    """
    from ..supervisor import get_current

    results: list[str | None] = [None] * len(indexed_items)
    sup = get_current()
    if sup is not None:
        futures = {
            sup.spawn_from_thread(coro_factory(idx, item), kind="subagent", label=f"task#{idx}"): idx
            for idx, item in indexed_items
        }
        for future, idx in futures.items():
            try:
                results[idx - 1] = future.result()
            except snippets.SnippetError:
                raise
            except Exception as exc:  # noqa: BLE001
                results[idx - 1] = f"ERROR {type(exc).__name__}: {exc}"
        return results

    async def _gather():
        return await asyncio.gather(
            *(coro_factory(idx, item) for idx, item in indexed_items),
            return_exceptions=True,
        )

    for (idx, _item), res in zip(indexed_items, asyncio.run(_gather())):
        if isinstance(res, snippets.SnippetError):
            raise res
        results[idx - 1] = (
            f"ERROR {type(res).__name__}: {res}"
            if isinstance(res, (Exception, asyncio.CancelledError))
            else res
        )
    return results


async def _fan_out_async(indexed_items: list[tuple[int, Any]], coro_factory) -> list[str | None]:
    """On-loop sibling of `_fan_out` for the REPL. The CALLER is
    already a coroutine on the shared loop (the parent turn), so each child turn
    is scheduled as a cancelable "subagent" job on THAT loop and awaited with
    `asyncio.gather` — holding NO dispatch-pool thread while the children run.

    This is the invariant that keeps a deeply nested fan-out from deadlocking the
    bounded js-dispatch executor: a blocked parent no longer parks a worker
    thread that its own descendants need for their leaf tool dispatch. Falls back
    to a plain on-loop gather when no supervisor is live (kept for symmetry)."""
    from ..supervisor import get_current

    results: list[str | None] = [None] * len(indexed_items)
    sup = get_current()
    if sup is not None:
        jobs = [
            (idx, sup.spawn(coro_factory(idx, item), kind="subagent", label=f"task#{idx}"))
            for idx, item in indexed_items
        ]
        gathered = await asyncio.gather(
            *(job.task for _idx, job in jobs), return_exceptions=True
        )
        for (idx, _job), res in zip(jobs, gathered):
            if isinstance(res, snippets.SnippetError):
                raise res
            results[idx - 1] = (
                f"ERROR {type(res).__name__}: {res}"
                if isinstance(res, BaseException)
                else res
            )
        return results

    gathered = await asyncio.gather(
        *(coro_factory(idx, item) for idx, item in indexed_items),
        return_exceptions=True,
    )
    for (idx, _item), res in zip(indexed_items, gathered):
        if isinstance(res, snippets.SnippetError):
            raise res
        results[idx - 1] = (
            f"ERROR {type(res).__name__}: {res}"
            if isinstance(res, BaseException)
            else res
        )
    return results


def _subagent_worker_cap(context: ToolContext) -> int:
    """SUBAGENT_WORKERS_MAX as ``context`` has it: the most subagents running at
    once in the whole process."""
    default = _settings.default_value("SUBAGENT_WORKERS_MAX")
    return int_or_default(context.subagent_max_workers, default, minimum=1)


def _prepare_fan_out(
    tasks: list[Any],
    agent_id: str,
    session_id: str | None,
    model: str,
    context: ToolContext,
    reasoning: str = "",
    kind: str = "task",
) -> tuple[str, None] | tuple[None, tuple[list[tuple[int, Any]], Any]]:
    """Validate a task/named-agent call and build (indexed_items, coro_factory).

    Returns ``(error_message, None)`` on a validation/depth failure, else
    ``(None, (indexed_items, coro_factory))``. Shared verbatim by the sync
    ``task`` (``-p``/bench) and async ``task_async`` (REPL) paths so
    the two rails can never drift. Each coroutine takes a slot of the
    process-wide gate, so subagents at every depth and from every call together
    never run more than SUBAGENT_WORKERS_MAX at once; a subagent waiting on its
    own subagents lends them its slot."""
    if not agent_id:
        return "ERROR: task requires agent_id", None
    if not isinstance(tasks, list):
        return "ERROR: task tasks must be a list of strings", None
    if any(not isinstance(item, str) for item in tasks):
        return "ERROR: task tasks must be strings", None
    normalized_items = [item.strip() for item in tasks if item.strip()]
    if not normalized_items:
        return "ERROR: task requires at least one non-empty task", None
    if session_id and len(normalized_items) > 1:
        return (
            "ERROR: session_id runs one task per session. Pass a single task, or omit "
            "session_id so each parallel worker gets its own fresh session.",
            None,
        )

    default_depth = _settings.default_value("SUBAGENT_DEPTH_MAX")
    max_depth = int_or_default(context.task_max_depth, default_depth, minimum=1)
    if context.task_depth >= max_depth:
        return f"ERROR: task recursion depth limit reached ({max_depth})", None

    from ..config import from_env, validate_agent_id
    from .registry import build_default_registry

    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        return f"ERROR: {exc}", None

    assembly = context.subagent_assembly or SubagentAssembly()
    config_factory = from_env if assembly.config_factory is None else assembly.config_factory
    registry_factory = build_default_registry if assembly.registry_factory is None else assembly.registry_factory
    worker = _run_one_task_async if assembly.worker is None else assembly.worker
    parent_cfg = context.config or config_factory(save_session=True)
    # Honor the subagent-model lock at every depth: when locked, rebuild the
    # nested registry without the `model_override` flag so the task tool's
    # `model` param is gone from the schema/description, not just ignored
    # (mirrors run_assembly.registry_for).
    reg_flags = () if getattr(parent_cfg, "lock_subagent_model", False) else ("model_override",)
    full_registry = registry_factory(getattr(parent_cfg, "prompt_roots", None), flags=reg_flags)
    total = len(normalized_items)
    limit = _subagent_worker_cap(context)

    def _coro(idx: int, item: Any):
        async def _gated() -> str:
            _LAUNCH_KIND.set(kind)
            await _GATE.acquire(limit)
            slot = _Slot(limit)
            _SLOT.set(slot)
            try:
                return await worker(
                    idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model,
                    **({"reasoning": reasoning} if reasoning else {}),
                )
            finally:
                slot.active = False
                if slot.held:
                    slot.held = False
                    _GATE.release()

        return _gated()

    indexed_items = [(idx, item) for idx, item in enumerate(normalized_items, 1)]
    return None, (indexed_items, _coro)


def _assemble_task_results(results: list[str | None], agent_id: str, session_id: str | None) -> str:
    """One task returns the worker's text verbatim, so an agent whose contract is
    "reply with exactly X" round-trips. A fan-out is numbered in task order under
    one header."""
    filled = [result if result is not None else "ERROR worker did not return" for result in results]
    if len(filled) == 1:
        return filled[0]
    numbered = [f"{idx}. {text}" for idx, text in enumerate(filled, 1)]
    header = f"TASK_RESULTS agent={agent_id}"
    if session_id:
        header += f" session_id={session_id}"
    return "\n\n".join([header, *numbered])


def _flag(raw: Any) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _job_action(action: Any) -> str:
    return (text_or_default(action, "run") or "run").strip().lower()


def _find_job(action: str, handle: Any, context: ToolContext) -> tuple[str | None, task_jobs.TaskJob | None]:
    if action not in ("poll", "wait", "kill"):
        return f"ERROR: unknown action {action!r}; expected run, poll, wait, or kill", None
    job = task_jobs.find(text_or_default(handle) or None, context.task_owner)
    if job is None:
        return f"ERROR: no background task{' ' + str(handle) if handle else ''} to {action}", None
    return None, job


def _wait_limit(timeout: Any) -> float | None:
    """Seconds a wait blocks; None (no timeout given) waits for the end."""
    if timeout is None:
        return None
    return float(int_or_default(timeout, 0, minimum=0))


def _start_background(indexed_items, coro_factory, agent_id: str, session_id: str | None,
                      context: ToolContext, *, on_loop: bool) -> str:
    job = task_jobs.start(
        indexed_items, coro_factory,
        lambda results: _assemble_task_results(results, agent_id, session_id),
        agent_id=agent_id,
        depth=context.task_depth,
        owner=context.task_owner,
        # A subagent can write anything, so the parent's memoized fs_search
        # results are stale once it is done.
        on_done=context.invalidate_search_cache,
        on_loop=on_loop,
    )
    return task_jobs.started_text(job)


def task(
    tasks: list[Any] | None = None,
    agent_id: str = "",
    session_id: str | None = None,
    model: str = "",
    background: bool = False,
    action: str = "run",
    handle: str | None = None,
    timeout: int | None = None,
    context: ToolContext | None = None,
    reasoning: str = "",
) -> str:
    return _run_task(tasks, agent_id, session_id, model, background, action, handle, timeout, context,
                     reasoning, launch="task")


def _run_task(tasks, agent_id, session_id, model, background, action, handle, timeout, context, reasoning,
              *, launch: str) -> str:
    """`task`, launched as ``launch`` ("task" or "agent")."""
    assert context is not None
    action = _job_action(action)
    if action != "run":
        error, job = _find_job(action, handle, context)
        if job is None:
            return error  # type: ignore[return-value]
        was_running = job.running()
        if action == "kill":
            job.kill()
            task_jobs.wait(job, task_jobs.KILL_GRACE_S)
        elif action == "wait":
            task_jobs.wait(job, _wait_limit(timeout))
        return task_jobs.report(job, action, was_running=was_running)
    agent_id = str(agent_id).strip()
    model = str(model or "").strip()
    reasoning = str(reasoning or "").strip().lower()
    kind = "background" if _flag(background) else launch
    error, prepared = _prepare_fan_out(tasks, agent_id, session_id, model, context, reasoning, kind)
    if prepared is None:
        return error  # type: ignore[return-value]
    indexed_items, coro_factory = prepared
    if _flag(background):
        return _start_background(indexed_items, coro_factory, agent_id, session_id, context, on_loop=False)
    results = _fan_out(indexed_items, coro_factory)
    # A subagent runs with its own ToolContext and can write anything under any
    # root, so the parent's memoized fs_search results are no longer trustworthy.
    context.invalidate_search_cache()
    return _assemble_task_results(results, agent_id, session_id)


task._js_fan_out = True  # type: ignore[attr-defined]


async def task_async(
    tasks: list[Any] | None = None,
    agent_id: str = "",
    session_id: str | None = None,
    model: str = "",
    background: bool = False,
    action: str = "run",
    handle: str | None = None,
    timeout: int | None = None,
    context: ToolContext | None = None,
    reasoning: str = "",
    launch: str = "task",
) -> str:
    """Async twin of :func:`task`: awaits child turns
    on the caller's loop via :func:`_fan_out_async` instead of blocking a
    dispatch thread. The runtime routes fan-out calls here
    (see ``turn_dispatch.dispatch_batch``). ``launch`` is "agent" for a
    named-agent tool's call."""
    assert context is not None
    action = _job_action(action)
    if action != "run":
        error, job = _find_job(action, handle, context)
        if job is None:
            return error  # type: ignore[return-value]
        was_running = job.running()
        if action == "kill":
            job.kill()
            await task_jobs.wait_async(job, task_jobs.KILL_GRACE_S)
        elif action == "wait":
            limit = _wait_limit(timeout)
            # A wait to the end lends this subagent's slot to the job; a timed
            # wait keeps it, so it returns when its time is up.
            async with _lend_slot() if limit is None else nullcontext():
                await task_jobs.wait_async(job, limit)
        return task_jobs.report(job, action, was_running=was_running)
    agent_id = str(agent_id).strip()
    model = str(model or "").strip()
    reasoning = str(reasoning or "").strip().lower()
    kind = "background" if _flag(background) else launch
    error, prepared = _prepare_fan_out(tasks, agent_id, session_id, model, context, reasoning, kind)
    if prepared is None:
        return error  # type: ignore[return-value]
    indexed_items, coro_factory = prepared
    if _flag(background):
        return _start_background(indexed_items, coro_factory, agent_id, session_id, context, on_loop=True)
    async with _lend_slot():
        results = await _fan_out_async(indexed_items, coro_factory)
    context.invalidate_search_cache()
    return _assemble_task_results(results, agent_id, session_id)


def is_fan_out_handler(handler: Any) -> bool:
    """True for a tool handler that spawns child turns and blocks on them (the
    `task` tool and every named-agent tool). The runtime routes these on-loop
    under a supervisor to avoid the dispatch-pool inversion."""
    return bool(getattr(handler, "_js_fan_out", False))


async def dispatch_fan_out_async(tool: Tool, args: dict[str, Any], context: ToolContext) -> str:
    """Await one fan-out tool call on the current loop. Bridges the runtime's
    on-loop dispatch to :func:`task_async`, resolving the worker ``agent_id`` from
    a named-agent tag when present, else from the call args."""
    handler = tool.handler
    agent_id = getattr(handler, "_js_agent_id", None)
    launch = "task" if agent_id is None else "agent"
    if agent_id is None:
        agent_id = str(args.get("agent_id", "")).strip()
    return await task_async(
        tasks=args.get("tasks"),
        agent_id=agent_id,
        session_id=args.get("session_id"),
        model=args.get("model", ""),
        background=args.get("background", False),
        action=args.get("action", "run"),
        handle=args.get("handle"),
        timeout=args.get("timeout"),
        context=context,
        reasoning=args.get("reasoning", ""),
        launch=launch,
    )


def named_agent_tool(agent_id: str, description: str | None = None) -> Tool:
    def run_named_agent(tasks: list[Any], model: str = "", reasoning: str = "",
                        context: ToolContext | None = None) -> str:
        return _run_task(tasks, agent_id, None, model, False, "run", None, None, context, reasoning,
                         launch="agent")

    # Tag so the runtime recognizes this as fan-out and awaits it on-loop under a
    # supervisor (like the `task` tool); `_js_agent_id` carries the fixed worker.
    run_named_agent._js_fan_out = True  # type: ignore[attr-defined]
    run_named_agent._js_agent_id = agent_id  # type: ignore[attr-defined]

    text = description or (
        snippets.substitute(load_description("named_agent"), {"AGENT_ID": agent_id})
    )
    return Tool(
        agent_id,
        text,
        run_named_agent,
        {"tasks": {"type": "array", "items": {"type": "string"}, "description": load_parameter_description('named_agent', 'tasks')},
         "model": {"type": "string", "description": load_parameter_description('named_agent', 'model')},
         "reasoning": {"type": "string", "description": load_parameter_description('named_agent', 'reasoning')}},
        required=("tasks",),
    )


def _task_params(flags: tuple[str, ...]) -> dict:
    params = {
        "tasks": {"type": "array", "items": {"type": "string"}, "description": load_parameter_description('task', 'tasks')},
        "agent_id": {"type": "string", "description": load_parameter_description('task', 'agent_id')},
        "session_id": {"type": "string", "description": load_parameter_description('task', 'session_id')},
        "background": {"type": "boolean", "default": False, "description": load_parameter_description('task', 'background')},
        "action": {"type": "string", "enum": ["run", "poll", "wait", "kill"], "default": "run", "description": load_parameter_description('task', 'action')},
        "handle": {"type": "string", "description": load_parameter_description('task', 'handle')},
        "timeout": {"type": "integer", "description": load_parameter_description('task', 'timeout')},
    }
    if "model_override" in flags:
        params["model"] = {"type": "string", "description": load_parameter_description('task', 'model')}
    return params


def tools(flags: tuple[str, ...] = ("model_override",)) -> tuple[Tool, ...]:
    return (
        Tool(
            "task",
            load_description("task", flags=flags),
            task,
            _task_params(flags),
            presentation=Presentation(key=("agent_id",)),
        ),
    )
