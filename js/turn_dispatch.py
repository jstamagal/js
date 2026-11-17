"""A turn's batch of tool calls run: each call on the thread or loop its tool needs, reads beside reads and writes alone, fan-out calls on the loop, results capped as they return."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import json
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import (
    console,
    held,
    jail as _jail,
    output,
    result_caps,
    snippets,
    tool_calls,
    tool_errors,
    toolkit as T,
)
from .tool_calls import PendingToolCall
from .toolkit import file_search, python_kernel, shell, subagents
from .toolkit.core import (
    ToolContext,
    ToolOutput,
    as_output,
    call_is_read_only,
    call_scope,
    call_tool,
    call_tool_async,
    call_view,
    registry_scope,
)
from .toolkit.registry import ToolRegistry

if TYPE_CHECKING:
    from .runtime import Telemetry


def reconcile_read_delivery(
    tool_name: str, args: dict, raw: Any, delivered: Any, context: ToolContext, call_id: str,
) -> None:
    """Match read coverage to what the model actually received.

    A clipping cap (inline spill, per-result cap, or the per-turn batch cap) runs
    after the read handler returned the full text and recorded coverage for all
    of it. Without this, patch is authorized to edit lines the model never saw.
    ``call_id`` names the read call whose coverage is narrowed."""
    if tool_name != "read" or not isinstance(raw, str) or not isinstance(delivered, str):
        return
    if delivered == raw:
        return
    raw_path = args.get("file_path") or args.get("path")
    if not isinstance(raw_path, str):
        return
    # A byte-range read has no line-number prefixes to recover what was
    # delivered, so a clipped one keeps none of its own coverage.
    bounds = args.get("range") if isinstance(args.get("range"), dict) else args
    byte_read = bounds.get("start_byte") is not None or bounds.get("end_byte") is not None
    context.reads.narrow_to_delivered(context.resolve_path(raw_path), raw, "" if byte_read else delivered, call_id)


def cell_call_observer(telemetry: Telemetry, trace: bool,
                        context: ToolContext | None) -> Callable[..., None]:
    """Trace and log a tool call a kernel cell makes while this call runs,
    the way a direct call is traced and logged."""

    def observe(name: str, args: dict, result: Any, seconds: float, failure: str | None,
                refused: bool = False) -> None:
        latency_ms = int(seconds * 1000)
        if refused:
            telemetry.event("tool_call_refused", tool=name, via="kernel", refusal=result)
        elif failure is None:
            telemetry.event("tool_ok", tool=name, via="kernel", latency_ms=latency_ms)
        else:
            telemetry.event("tool_exception", tool=name, via="kernel", error=failure,
                            latency_ms=latency_ms)
        console.trace_result(telemetry, context, name, result, args=args, traced=trace, view=call_view())

    return observe


@dataclass(frozen=True)
class _CallWay:
    """How one way of running a tool call differs from the others."""
    # Runs the handler: (tool, args, context) -> awaitable of its result.
    invoke: Callable[[Any, dict, ToolContext], Awaitable[Any]]
    # The handler runs under the turn's output sink, `call_scope` and `registry_scope`.
    scoped: bool = True
    # A read's coverage is narrowed to what the cap let through.
    reconcile_reads: bool = True
    # A call of an unknown `write` holds its content.
    hold_unknown_write: bool = False


async def _call_on_this_thread(tool: Any, args: dict, context: ToolContext) -> Any:
    return call_tool(tool, args, context)


async def _call_fan_out(tool: Any, args: dict, context: ToolContext) -> Any:
    return await subagents.dispatch_fan_out_async(tool, args, context)


# A sync handler on a dispatch worker thread.
_ON_THREAD = _CallWay(_call_on_this_thread, hold_unknown_write=True)


# An async handler awaited on the loop.
_ON_LOOP = _CallWay(call_tool_async)


# A fan-out (task / named-agent) handler awaited on the loop.
_FAN_OUT = _CallWay(_call_fan_out, scoped=False, reconcile_reads=False)


@contextlib.contextmanager
def _call_scopes(telemetry: Telemetry, trace: bool, call_id: str, registry: ToolRegistry,
                 context: ToolContext) -> Iterator[None]:
    with output.using_sink(output.sink_of(telemetry)), call_scope(call_id), registry_scope(
            registry, cell_call_observer(telemetry, trace, context)):
        yield


async def _run_call(way: _CallWay, name: str, raw_args: str, *, call_id: str, called_name: str | None,
                    telemetry: Telemetry, cap_bytes: int, trace: bool, trace_together: bool,
                    error_tracker: tool_errors.ToolErrorTracker | None, registry: ToolRegistry,
                    context: ToolContext) -> tuple[dict, Any]:
    """One tool call from the model's raw arguments to the result the model
    gets: parse, resolve, trace, log, run the handler ``way`` says, cap,
    count errors, narrow read coverage. Returns (parsed_args, result)."""
    logged_name = called_name or name
    try:
        args = tool_calls.repair_jsonish(raw_args)
    except ValueError as exc:
        opened = console.trace_call(telemetry, context, name, None, malformed=True,
                             traced=trace, deferred=trace_together)
        telemetry.event("tool_error", tool=logged_name, call_id=call_id, error=f"argparse: {exc}")
        result = f'ERROR: could not parse arguments for {name}: {exc}'
        if error_tracker is not None:
            result = error_tracker.record(name, result)
        console.trace_result(telemetry, context, name, result, malformed=True, exchange=opened, traced=trace)
        return {}, result

    tool = registry.resolve(name)
    opened = console.trace_call(telemetry, context, tool.name if tool is not None else name, args,
                         traced=trace, deferred=trace_together)
    if tool is None:
        telemetry.event("tool_unknown", tool=logged_name, call_id=call_id, args=args)
        result = registry.unavailable_error(name)
        if way.hold_unknown_write and tool_calls.canonical_tool_call_name(name, registry) == "write":
            result += held.hold_failed_call(context, raw_args, args)
        result = result_caps.cap_result(result, cap_bytes, context=context)
        console.trace_result(telemetry, context, name, result, args=args, exchange=opened, traced=trace)
        return args, result

    started = time.time()
    view = None
    try:
        with (_call_scopes(telemetry, trace, call_id, registry, context) if way.scoped
              else contextlib.nullcontext()):
            result = await way.invoke(tool, args, context)
            if way.scoped:
                view = call_view()
        telemetry.event("tool_ok", tool=logged_name, call_id=call_id,
                        latency_ms=int((time.time() - started) * 1000))
    except snippets.SnippetError:
        raise
    except Exception as exc:  # noqa: BLE001
        telemetry.event("tool_exception", tool=logged_name, call_id=call_id,
                        error=f"{type(exc).__name__}: {exc}",
                        latency_ms=int((time.time() - started) * 1000))
        result = _jail.shown(f'ERROR running {tool.name}: {type(exc).__name__}: {exc}')
    produced = as_output(result)
    result = produced.model
    # The tracker counts the whole error, and the cap is the last cut: an
    # error over the cap loses its retry note with the rest of its tail.
    if error_tracker is not None and isinstance(result, str):
        result = error_tracker.record(tool.name, result)
    capped = result_caps.cap_result(result, cap_bytes, context=context)
    if way.reconcile_reads:
        reconcile_read_delivery(tool.name, args, result, capped, context, call_id)
    console.trace_result(telemetry, context, tool.name, _operator_sees(produced, result), args=args,
                  exchange=opened, view=view, traced=trace)
    return args, capped


def _run_to_end(call: Coroutine[Any, Any, Any]) -> Any:
    """The value of ``call``, run on this thread; a call made `_ON_THREAD`
    never suspends."""
    try:
        call.send(None)
    except StopIteration as done:
        return done.value
    call.close()
    raise RuntimeError("a tool call on a dispatch thread suspended")


def _dispatch(name: str, raw_args: str, telemetry: Telemetry,
              cap_bytes: int, trace: bool = False,
              error_tracker: tool_errors.ToolErrorTracker | None = None,
              registry: ToolRegistry | None = None,
              tool_context: ToolContext | None = None,
              trace_together: bool = False,
              call_id: str | None = None, called_name: str | None = None) -> tuple[dict, str]:
    """Parse + execute one tool call with a sync handler, on this thread.
    Returns (parsed_args, result_string).

    `trace_together` prints the call header with the result instead of before
    the call runs; concurrent calls use it so their exchanges do not interleave.
    `call_id` is the model's id for the call; read coverage is recorded under it."""
    return _run_to_end(_run_call(
        _ON_THREAD, name, raw_args, call_id=call_id or f"dispatch-{next(_ANONYMOUS_CALLS)}",
        called_name=called_name, telemetry=telemetry, cap_bytes=cap_bytes, trace=trace,
        trace_together=trace_together, error_tracker=error_tracker,
        registry=registry or T.stock_registry(), context=tool_context or T.STOCK_CONTEXT))


async def _dispatch_fan_out_async(
    pc: PendingToolCall,
    telemetry: Telemetry,
    cap_bytes: int,
    trace: bool,
    error_tracker: tool_errors.ToolErrorTracker,
    registry: ToolRegistry,
    tool_context: ToolContext,
    trace_together: bool = False,
) -> tuple[PendingToolCall, dict, str]:
    """Execute ONE fan-out (task / named-agent) tool call by awaiting its child
    turns on the current loop (never a dispatch thread)."""
    args, result = await _run_call(
        _FAN_OUT, pc.name, pc.arguments(), call_id=pc.id, called_name=pc.called_name,
        telemetry=telemetry, cap_bytes=cap_bytes, trace=trace, trace_together=trace_together,
        error_tracker=error_tracker, registry=registry, context=tool_context)
    return pc, args, result


async def _dispatch_async_tool(
    pc: PendingToolCall, telemetry: Telemetry, cap_bytes: int, trace: bool,
    error_tracker: tool_errors.ToolErrorTracker, registry: ToolRegistry, tool_context: ToolContext,
) -> tuple[PendingToolCall, dict, Any]:
    """Execute one call of an async handler on the current loop."""
    args, result = await _run_call(
        _ON_LOOP, pc.name, pc.arguments(), call_id=pc.id, called_name=pc.called_name,
        telemetry=telemetry, cap_bytes=cap_bytes, trace=trace, trace_together=False,
        error_tracker=error_tracker, registry=registry, context=tool_context)
    return pc, args, result


def _operator_sees(output: ToolOutput, model: Any) -> Any:
    """What the operator sees of a call: everything it produced
    (`ToolOutput.whole`), or, when the tool cut nothing itself, the result
    before TOOL_RESULT_MAX and the spill cut it for the model. ``model`` is
    that result, with the retry note the model gets."""
    return model if output.whole is None else output.whole


def _is_task_call(pc: PendingToolCall) -> bool:
    return pc.name.lower() == "task"


def _is_read_only_call(pc: PendingToolCall, registry: ToolRegistry) -> bool:
    """True when ``pc`` names a tool that writes nothing with these arguments.
    A call that cannot be resolved or parsed is not read-only."""
    tool = registry.resolve(pc.name)
    if tool is None or pc.validation_error is not None:
        return False
    try:
        args = tool_calls.repair_jsonish(pc.arguments())
    except ValueError:
        return False
    return call_is_read_only(tool, args)


def _read_write_groups(indices: list[int], read_only: Callable[[int], bool]) -> list[list[int]]:
    """Split calls, in model order, into the groups a readers-writer lock admits.

    Consecutive read-only calls share a group and run together. Every other
    call is a group of its own: it starts after every call before it has
    finished, and the calls after it wait for it. read, read, patch, read
    gives [read, read], [patch], [read]."""
    groups: list[list[int]] = []
    readers: list[int] = []
    for idx in indices:
        if read_only(idx):
            readers.append(idx)
            continue
        if readers:
            groups.append(readers)
            readers = []
        groups.append([idx])
    if readers:
        groups.append(readers)
    return groups


_ANONYMOUS_CALLS = itertools.count()


@dataclass
class DispatchProgress:
    """Known outcomes survive cancellation; the stop flag gates queued leaves."""

    stopped: threading.Event = field(default_factory=threading.Event)
    records: dict[str, tuple[PendingToolCall, dict, Any]] = field(default_factory=dict)
    finished: dict[str, float] = field(default_factory=dict)   # call id -> when its result came

    def record(self, pc: PendingToolCall, args: dict, result: Any) -> None:
        self.records[pc.id] = (pc, args, result)
        self.finished[pc.id] = time.time()


def _dispatch_tool_calls(
    tool_calls: list[PendingToolCall],
    telemetry: Telemetry,
    cap_bytes: int,
    trace: bool,
    error_tracker: tool_errors.ToolErrorTracker,
    registry: ToolRegistry,
    tool_context: ToolContext,
    progress: DispatchProgress | None = None,
    trace_together: bool = False,
) -> list[tuple[PendingToolCall, dict, str]]:
    """Dispatch one assistant batch.

    All `task` calls from the same assistant turn run concurrently first. The
    other calls then run in model order under a readers-writer rule (see
    _read_write_groups): read-only calls run together, up to
    tool_context.max_parallel_tools at once, and a call that writes runs
    alone. Results come back in the original order. `trace_together` is set
    when other calls run beside this batch; calls that run concurrently always
    trace together.
    """
    records: list[tuple[dict, str] | None] = [None] * len(tool_calls)
    for idx, pc in enumerate(tool_calls):
        if pc.validation_error is None:
            continue
        args = json.loads(pc.arguments())
        telemetry.event("tool_unknown" if pc.unavailable else "tool_invalid",
                        tool=pc.called_name or pc.name, call_id=pc.id, error=pc.validation_error)
        result = pc.validation_error if pc.unavailable or pc.refused else f'ERROR: invalid arguments for {pc.name}: {pc.validation_error}'
        recorded = result_caps.cap_result(result, cap_bytes, context=tool_context)
        if not pc.unavailable:
            recorded = error_tracker.record(pc.name, recorded)
        # A call rejected before dispatch is still an exchange the model sees.
        # It used to be invisible in the trace from both ends.
        console.trace_result(telemetry, tool_context, pc.name, recorded, args=args, traced=trace)
        records[idx] = (args, recorded)
        if progress is not None:
            progress.record(pc, args, recorded)
    task_indices = [
        idx for idx, pc in enumerate(tool_calls)
        if records[idx] is None and _is_task_call(pc)
    ]

    if task_indices:
        with ThreadPoolExecutor(max_workers=len(task_indices), thread_name_prefix="js-runtime-task") as executor:
            futures = {
                idx: executor.submit(
                    _dispatch,
                    tool_calls[idx].name,
                    tool_calls[idx].arguments(),
                    telemetry,
                    cap_bytes,
                    trace,
                    None,
                    registry,
                    tool_context,
                    trace_together or len(task_indices) > 1,
                    tool_calls[idx].id, tool_calls[idx].called_name,
                )
                for idx in task_indices
            }
            for idx in task_indices:
                try:
                    args, result = futures[idx].result()
                except snippets.SnippetError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    args = {}
                    result = f'ERROR running task: {type(exc).__name__}: {exc}'
                records[idx] = (args, error_tracker.record("task", result))
                if progress is not None:
                    progress.record(tool_calls[idx], *records[idx])

    def stopped() -> bool:
        return progress is not None and progress.stopped.is_set()

    def run_leaf(idx: int, together: bool) -> None:
        # A queued call checks the stop flag as it starts: ^C lets the calls
        # already running finish and starts no new one.
        if stopped():
            return
        pc = tool_calls[idx]
        records[idx] = _dispatch(
            pc.name,
            pc.arguments(),
            telemetry,
            cap_bytes,
            trace,
            error_tracker,
            registry,
            tool_context,
            together,
            pc.id, pc.called_name,
        )
        if progress is not None:
            progress.record(pc, *records[idx])

    limit = max(1, int(getattr(tool_context, "max_parallel_tools", 1) or 1))
    leaves = [idx for idx in range(len(tool_calls)) if records[idx] is None]
    groups = _read_write_groups(
        leaves, lambda idx: limit > 1 and _is_read_only_call(tool_calls[idx], registry),
    )
    for group in groups:
        if stopped():
            break
        if len(group) == 1:
            run_leaf(group[0], trace_together)
            continue
        with ThreadPoolExecutor(max_workers=min(limit, len(group)),
                                thread_name_prefix="js-runtime-read") as executor:
            futures = [executor.submit(run_leaf, idx, True) for idx in group]
        for future in futures:
            future.result()

    return [
        (pc, *record)
        for pc, record in zip(tool_calls, records, strict=True)
        if record is not None
    ]


def _interrupt_inflight(tool_context: ToolContext) -> None:
    """Stop the external process a cancelled turn's tool call is blocked on.

    A worker thread running a sync tool cannot be cancelled, so a cancelled turn
    leaves the call running to its own deadline. The kernel tool owns a process
    that outlives the call, and a cell left executing there would sit behind the
    next call; interrupting it makes the drain short and the kernel idle. A
    shell call blocked on its command gets that command's process tree killed,
    so ^C ends the turn now rather than when the shell wait runs out.
    """

    if tool_context.tool_approval is not None:
        tool_context.tool_approval.cancel()
    python_kernel.interrupt_inflight(tool_context)
    shell.interrupt_inflight(tool_context)
    file_search.interrupt_inflight(tool_context)


async def dispatch_batch(
    tool_calls: list[PendingToolCall],
    telemetry: Telemetry,
    cap_bytes: int,
    trace: bool,
    error_tracker: tool_errors.ToolErrorTracker,
    registry: ToolRegistry,
    tool_context: ToolContext,
    loop: asyncio.AbstractEventLoop,
    progress: DispatchProgress | None = None,
) -> list[tuple[PendingToolCall, dict, Any]]:
    """Drain running sync leaves on cancel, cancel async work, retain outcomes.

    Fan-out stays on the event loop to avoid blocking descendant dispatch on
    an ancestor's worker. Mixed async batches retain model order; pure fan-out
    batches run concurrently with their sync leaves, as before.
    """

    progress = progress if progress is not None else DispatchProgress()
    fan_out = set()
    async_leaves = set()
    for i, pc in enumerate(tool_calls):
        tool = registry.resolve(pc.name)
        if tool is None or pc.validation_error is not None:
            continue
        if subagents.is_fan_out_handler(tool.handler):
            fan_out.add(i)
        elif inspect.iscoroutinefunction(tool.handler):
            async_leaves.add(i)

    async def sync_calls(calls: list[PendingToolCall]) -> None:
        if not calls or progress.stopped.is_set():
            return
        # A running Python worker cannot be cancelled. Keep its future alive,
        # stop its queued calls, and collect its result before the caller saves
        # the interrupted turn. Never block the event loop with shutdown(wait).
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="js-runtime-leaf") as executor:
            future = loop.run_in_executor(
                executor, _dispatch_tool_calls, calls, telemetry, cap_bytes,
                trace, error_tracker, registry, tool_context, progress, concurrent,
            )
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                progress.stopped.set()
                _interrupt_inflight(tool_context)
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                future.result()
                raise

    async def async_call(i: int) -> None:
        if progress.stopped.is_set():
            return
        if i in fan_out:
            record = await _dispatch_fan_out_async(tool_calls[i], telemetry, cap_bytes, trace,
                                                   error_tracker, registry, tool_context, concurrent)
        else:
            record = await _dispatch_async_tool(tool_calls[i], telemetry, cap_bytes, trace,
                                                error_tracker, registry, tool_context)
        progress.record(*record)

    # Pure fan-out batches run every call at once; their exchanges print whole.
    concurrent = bool(fan_out) and not async_leaves and len(tool_calls) > 1

    jobs: list[asyncio.Task] = []
    try:
        if async_leaves:
            pending = []
            for i, pc in enumerate(tool_calls):
                if i in fan_out or i in async_leaves:
                    await sync_calls(pending)
                    pending = []
                    await async_call(i)
                else:
                    pending.append(pc)
            await sync_calls(pending)
        elif fan_out:
            jobs = [asyncio.create_task(async_call(i)) for i in sorted(fan_out)]
            jobs.append(asyncio.create_task(sync_calls(
                [pc for i, pc in enumerate(tool_calls) if i not in fan_out]
            )))
            await asyncio.gather(*jobs)
        else:
            await sync_calls(tool_calls)
    except BaseException:
        progress.stopped.set()
        for job in jobs:
            job.cancel()
        if jobs:
            drain = asyncio.gather(*jobs, return_exceptions=True)
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    continue
        raise
    return [progress.records[pc.id] for pc in tool_calls if pc.id in progress.records]
