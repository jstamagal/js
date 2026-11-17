"""Every agent of a swarm in one process: ``python -m js.swarm run SPEC.json``.

SPEC = {"root": ROOT, "slots": 0,
        "agents": [{"name": "kivu", "agent": "troop", "model": "cpa/deepseek-v4.1-flash",
                    "effort": "low", "opener": "You are kivu ...", "session": "troop-1-kivu",
                    "cwd": "/path", "seat_file": "/path/kivu.seat.md", "opts": {"limits.x": "1"}}]}

Each agent is the task tool's child turn made long-lived: its own config, prompt,
session and tool context, the bus tools on its surface, its inbox drained at
every tool boundary, the turn persisted and the compaction trigger run before
every sleep, and the sleep an await on the inbox. ``slots`` caps how many agents
are mid-turn at once; 0 is no cap. ``recruit`` adds an agent to this loop.

Two message kinds are the runner's, not the agent's (``swarm.RUNNER_KINDS``):
``cancel`` sent to an agent cancels its current turn, saved as it stands, and
the agent goes on as after any turn (it ends there when it has seen a
``stop``); ``restart`` sent to an agent that has ended starts it again from its
spec, continuing its session, with the message's body as its first message (its
opener when the body is empty). Sent to an agent that is not in that state,
either is dropped.

Events go to stdout as JSON lines, every one carrying ``agent``; ``agent_start``,
``agent_end``, ``turn_cancelled``, ``restart``, ``dropped`` and ``run_end`` are
the runner's own. The process ends when every agent has stopped or retired. SIGTERM and SIGINT end it like ^C: every session
is persisted and the exit code is 130.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TextIO

from . import (
    agent_messages,
    compaction,
    headless,
    messages as msgs,
    run_assembly,
    runtime,
    settings as _settings,
    swarm,
)
from .config import InvocationSources, from_env
from .conversation import Conversation
from .session_catalog import record_session_start
from .toolkit import bus_tools
from .toolkit.core import ToolContext
from .toolkit.terminal import close_terminal_sessions


@dataclass
class AgentSpec:
    name: str
    opener: str
    agent: str | None = None      # None: the configured default agent, as plain `js -p` resolves it
    model: str | None = None
    effort: str | None = None
    session: str | None = None
    cwd: str = ""
    seat_file: str = ""
    opts: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> AgentSpec:
        return cls(
            name=swarm.check_name(str(d.get("name") or "")),
            opener=str(d.get("opener") or ""),
            agent=str(d.get("agent") or "").strip() or None,
            model=str(d["model"]).strip() or None if d.get("model") else None,
            effort=str(d["effort"]).strip() or None if d.get("effort") else None,
            session=str(d["session"]).strip() or None if d.get("session") else None,
            cwd=str(d.get("cwd") or ""),
            seat_file=str(d.get("seat_file") or ""),
            opts={str(k): v for k, v in (d.get("opts") or {}).items()},
        )


class AgentEvents(headless.JsonEvents):
    """One agent's event stream, every line stamped with its name, sharing the run's lock."""

    def __init__(self, out: TextIO, lock: threading.Lock, agent: str) -> None:
        super().__init__(out)
        self._lock = lock
        self.agent = agent

    def emit(self, kind: str, **fields: Any) -> None:
        super().emit(kind, agent=self.agent, **fields)


def _slots(cfg: Any, spec_slots: Any) -> int:
    if spec_slots is not None:
        return max(0, int(spec_slots))
    settings = getattr(cfg, "settings", {}) or {}
    raw = settings.get("SWARM_ACTIVE_MAX")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _configure(spec: AgentSpec) -> run_assembly.SettledRun:
    """The settled run of one agent: the bus tools on its surface and its
    seat file after its system prompt."""
    extras = tuple(f"{k}={v}" for k, v in spec.opts.items())
    cwd = Path(spec.cwd).expanduser() if spec.cwd else Path.cwd()
    cfg = from_env(save_session=True, sources=InvocationSources(extras=extras), agent_id=spec.agent,
                   session=spec.session, cwd=cwd)
    if spec.effort is not None:
        # The spec's own effort is set for this run like a flag; agent.yaml's
        # effort is then left out of the agent layer.
        effort, error = run_assembly.check_effort(spec.effort)
        if error:
            raise ValueError(error)
        cfg = replace(cfg, layer_flag={**(cfg.layer_flag or {}), "EFFORT": effort})
    try:
        settled = run_assembly.settle(cfg, model=spec.model, agent_reasoning=spec.effort is None)
    except run_assembly.RunNotSettled as exc:
        raise exc.cause from None
    system = settled.system
    if spec.seat_file:
        system = system.rstrip() + "\n\n" + Path(spec.seat_file).expanduser().read_text(encoding="utf-8")
    return replace(settled, system=system, registry=bus_tools.with_bus_tools(settled.registry))


class Runner:
    def __init__(self, root: Path, specs: list[AgentSpec], slots: int | None, out: TextIO, *, configure=None) -> None:
        self.configure = configure
        self.root = root
        self.specs = specs
        self.spec_slots = slots
        self.out = out
        self.lock = threading.Lock()
        self.tasks: dict[str, asyncio.Task] = {}
        # Each agent's turn in flight, which `cancel` cancels.
        self.turns: dict[str, asyncio.Task] = {}
        # The turns a `cancel` ended: only those end alone.
        self.cancelled: set[asyncio.Task] = set()
        # Each agent's spec as first given, and, once it has ended, the spec a
        # `restart` runs: the same, on the session it used.
        self.originals: dict[str, AgentSpec] = {}
        self.ended: dict[str, AgentSpec] = {}
        self.gate: Any = None
        self.code = 0

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.cancel_all)
        for spec in self.specs:
            self.add(spec)
        watcher = loop.create_task(self.watch(), name="swarm:runner")
        try:
            while self.tasks:
                await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
                for name in [n for n, t in self.tasks.items() if t.done()]:
                    self.tasks.pop(name, None)
        finally:
            watcher.cancel()
        AgentEvents(self.out, self.lock, "").emit("run_end", agents=[s.name for s in self.specs],
                                                   exit_code=self.code)
        return self.code

    def add(self, spec: AgentSpec) -> asyncio.Task:
        self.originals.setdefault(spec.name, spec)
        self.ended.pop(spec.name, None)
        task = asyncio.get_running_loop().create_task(self.run_agent(spec), name=f"swarm:{spec.name}")
        self.tasks[spec.name] = task
        return task

    async def watch(self) -> None:
        """Act on the runner's message kinds as they land, every poll."""
        room = swarm.Room(self.root)
        while True:
            for name in list(self.originals):
                landed = swarm.Inbox(room, name).take(swarm.RUNNER_KINDS)
                for msg in landed:
                    try:
                        self.act(name, msg)
                    except Exception as exc:  # noqa: BLE001 - one bad message does not stop the watch
                        AgentEvents(self.out, self.lock, name).emit(
                            "error", message=f"{type(exc).__name__}: {exc}", retryable=False)
            await asyncio.sleep(swarm.POLL_S)

    def act(self, name: str, msg: swarm.Msg) -> None:
        events = AgentEvents(self.out, self.lock, name)
        turn = self.turns.get(name)
        if msg.kind == swarm.CANCEL and turn is not None and not turn.done():
            self.cancelled.add(turn)
            turn.cancel()
            return
        if msg.kind == swarm.RESTART and name in self.ended:
            spec = self.ended[name]
            events.emit("restart", seq=msg.seq, sender=msg.sender)
            self.add(replace(spec, opener=msg.body or spec.opener))
            return
        events.emit("dropped", seq=msg.seq, message_kind=msg.kind, sender=msg.sender)

    async def turn(self, name: str, events: AgentEvents, turn: Any) -> None:
        """Run one turn as a task of its own, which `cancel` can end alone."""
        task = asyncio.ensure_future(turn)
        self.turns[name] = task
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task not in self.cancelled or (current is not None and current.cancelling()):
                raise
            events.emit("turn_cancelled")
        finally:
            self.turns.pop(name, None)
            self.cancelled.discard(task)

    def cancel_all(self) -> None:
        self.code = 130
        for task in self.tasks.values():
            task.cancel()

    def recruiter(self, parent: AgentSpec, agent: swarm.Agent):
        """What `recruit` calls on this runner, from a tool thread: a sibling
        coroutine with the parent's shape, scheduled onto the loop."""
        loop = asyncio.get_running_loop()

        def recruit(name: str, opener: str) -> None:
            swarm.check_name(name)
            home = self.root / name
            if name in agent.room.members() or name in self.tasks or (home / "spawned").is_file():
                raise ValueError(f"{name} is already on the bus")
            spawned = sum(1 for p in self.root.iterdir() if (p / "spawned").is_file()) if self.root.is_dir() else 0
            maximum = int(_settings.live_value("SWARM_SPAWN_MAX"))
            if spawned >= maximum:
                raise ValueError(f"this bus already has {spawned} recruited agents; the cap is {maximum}")
            child = replace(parent, name=name, opener=opener,
                            session=f"{parent.session}-{name}" if parent.session else None)
            home.mkdir(parents=True, exist_ok=True)
            (home / "spawned").write_text(json.dumps({"by": agent.name, "pid": os.getpid(), "inproc": True}),
                                          encoding="utf-8")
            self.specs.append(child)
            loop.call_soon_threadsafe(self.add, child)
        return recruit

    async def run_agent(self, spec: AgentSpec) -> None:
        events = AgentEvents(self.out, self.lock, spec.name)
        reason = "stopped"
        chat = None
        context = ToolContext(cwd=Path(spec.cwd).expanduser() if spec.cwd else Path.cwd())
        context.net_label = spec.name
        try:
            agent = swarm.Agent(self.root / spec.name)
            agent.recruiter = self.recruiter(spec, agent)
            context.swarm = agent
            configure = _configure if self.configure is None else self.configure
            settled = configure(spec)
            cfg, system = settled.config, settled.system
            if self.gate is None:
                slots = _slots(cfg, self.spec_slots)
                self.gate = asyncio.Semaphore(slots) if slots > 0 else contextlib.nullcontext()
            telemetry = runtime.Telemetry(debug_log=cfg.debug_log)

            async def gated(*args: Any, **kwargs: Any) -> None:
                async with self.gate:
                    await runtime.run_turn_async(*args, **kwargs)

            chat = Conversation(cfg, system, registry=settled.registry, context=context, telemetry=telemetry,
                                on_notice=lambda _notice: None, run_turn=gated)
            if not chat.messages and Path(cfg.session_file) != Path(os.devnull):
                record_session_start(cfg.session_file, cwd=context.cwd, agent=cfg.agent_id, model=cfg.model,
                                     provider=cfg.provider_id, mode="swarm")
            turn_kwargs = settled.turn_kwargs(suppress_output=True, trace_override=False, steer=agent.steer,
                                              event_sink=events.runtime_event)
            events.emit("agent_start", model=cfg.model, provider=cfg.provider_id, session=str(cfg.session_file),
                        cwd=str(context.cwd))
            await agent_messages.initialize(cfg, settled.prompt_spec, chat.messages, telemetry, system=system,
                                            tool_context=context, checkpoint=chat.save, **turn_kwargs)
            await self.turn(spec.name, events, chat.turn({"role": "user", "content": spec.opener}, **turn_kwargs))
            while not agent.stop_seen:
                with contextlib.suppress(Exception):
                    await chat.maybe_compact(emit=compaction.printing(), log_dir=compaction.flight_log_dir(cfg))
                events.emit("sleep")
                landed = await agent.sleep_async()
                events.emit("wake", count=len(landed), seqs=[m.seq for m in landed if m.seq],
                            kinds=sorted({m.kind for m in landed}))
                if all(m.kind == swarm.STOP for m in landed):
                    break
                await self.turn(spec.name, events, chat.turn(agent.wake_message(landed), **turn_kwargs))
            reason = "retired" if agent.retired else "stopped"
        except asyncio.CancelledError:
            reason = "cancelled"
        except Exception as exc:  # noqa: BLE001 - one agent's failure ends that agent, not the swarm
            reason = "error"
            if self.code == 0:
                self.code = 1
            events.emit("error", message=f"{type(exc).__name__}: {exc}", retryable=False)
        finally:
            original = self.originals.get(spec.name, spec)
            session = chat.cfg.session_file if chat is not None else None
            self.ended[spec.name] = replace(original, session=str(session)) if (
                session is not None and Path(session) != Path(os.devnull)) else original
            if chat is not None:
                with contextlib.suppress(Exception):
                    chat.save()
            close_terminal_sessions(context)
            events.emit("agent_end", reason=reason)


def load_spec(path: str | os.PathLike[str]) -> tuple[Path, list[AgentSpec], int | None]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    root = Path(str(raw.get("root") or "")).expanduser()
    if not str(raw.get("root") or ""):
        raise ValueError(msgs.SWARM_RUN_THE_SPEC_NEEDS_A_ROOT_THE_BUS.format())
    agents = [AgentSpec.from_dict(d) for d in raw.get("agents") or []]
    if not agents:
        raise ValueError(msgs.SWARM_RUN_THE_SPEC_NAMES_NO_AGENTS.format())
    names = [a.name for a in agents]
    if len(set(names)) != len(names):
        raise ValueError(msgs.SWARM_RUN_AGENT_NAMES_REPEAT.format(value=', '.join(n for n in names if names.count(n) > 1)))
    slots = raw.get("slots")
    return root, agents, None if slots is None else int(slots)


def run(path: str | os.PathLike[str], out: TextIO | None = None, *, configure=None) -> int:
    root, agents, slots = load_spec(path)
    runner = Runner(root, agents, slots, out or sys.stdout, configure=configure)
    return asyncio.run(runner.run())


def main(path: str) -> int:
    try:
        return run(path)
    except (OSError, ValueError) as exc:
        print(msgs.SWARM_RUN_FAILED.format(exc=exc), file=sys.stderr)
        return 2
