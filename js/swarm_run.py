"""The agents of a swarm on one loop per runner process: ``python -m js.swarm run SPEC.json``.

SPEC = {"root": ROOT, "slots": 0, "processes": 0,
        "agents": [{"name": "kivu", "agent": "troop", "model": "cpa/deepseek-v4.1-flash",
                    "effort": "low", "opener": "You are kivu ...", "session": "troop-1-kivu",
                    "cwd": "/path", "seat_file": "/path/kivu.seat.md", "opts": {"limits.x": "1"}}]}

Each agent is the task tool's child turn made long-lived: its own config, prompt,
session and tool context, the bus tools on its surface, its inbox drained at
every tool boundary, the turn persisted and the compaction trigger run before
every sleep, and the sleep an await on the inbox. ``slots`` caps how many agents
are mid-turn at once; 0 is no cap. ``recruit`` adds an agent to this loop.
``processes`` (else SWARM_PROCESSES) spreads the agents over that many runner
processes (`run_sharded`); 0 is one per AGENTS_PER_PROCESS, up to the CPUs.

Two message kinds are the runner's, not the agent's (``swarm.RUNNER_KINDS``):
``cancel`` sent to an agent cancels its current turn, saved as it stands, and
the agent goes on as after any turn (it ends there when it has seen a
``stop``); ``restart`` sent to an agent that has ended starts it again from its
spec, continuing its session, with the message's body as its first message (its
opener when the body is empty). Sent to an agent that is not in that state,
either is dropped.

Events go to stdout as JSON lines, every one carrying ``agent``; ``agent_start``,
``agent_end``, ``turn_cancelled``, ``restart``, ``dropped`` and ``run_end`` are
the runner's own. What an agent writes to stderr goes to stderr as
``[name] line`` and to ROOT/<name>/stderr (`AgentStderr`). A runner ends
when every agent has stopped or retired. SIGTERM and SIGINT end it like ^C:
every session is persisted and the exit code is 130.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TextIO

from . import (
    agent_messages,
    compaction,
    headless,
    messages as msgs,
    model_client,
    paths,
    run_assembly,
    runtime,
    settings as _settings,
    standalone_hooks,
    swarm,
)
from .config import InvocationSources, from_env, jsrc_paths
from .conversation import Conversation
from .session_catalog import record_session_start
from .toolkit import bus_tools, subagents
from .toolkit.core import ToolContext


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


# The agent whose task is running: what it writes to stderr is tagged with it.
_AGENT: ContextVar[str | None] = ContextVar("js_swarm_agent", default=None)


class AgentStderr:
    """``sys.stderr`` while the runner runs. A line an agent's task writes
    (a warning, an `on` handler's output, a traceback) goes to the process's
    stderr as ``[name] line`` and is appended to ROOT/<name>/stderr; what no
    agent writes passes through as it is."""

    def __init__(self, real: TextIO, root: Path) -> None:
        self._real, self._root = real, root
        self._partial: dict[str, str] = {}
        self._lock = threading.Lock()
        self.encoding = getattr(real, "encoding", "utf-8")

    def write(self, text: str) -> int:
        name = _AGENT.get()
        if name is None:
            return self._real.write(text)
        with self._lock:
            *lines, self._partial[name] = (self._partial.get(name, "") + text).split("\n")
            self._emit(name, lines)
        return len(text)

    def finish(self, name: str) -> None:
        """Write out a line the agent left unended."""
        with self._lock:
            rest = self._partial.pop(name, "")
            if rest:
                self._emit(name, [rest])

    def _emit(self, name: str, lines: list[str]) -> None:
        if not lines:
            return
        for line in lines:
            self._real.write(f"[{name}] {line}\n")
        with contextlib.suppress(OSError):
            path = self._root / name / "stderr"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", errors="backslashreplace") as f:
                f.write("".join(f"{line}\n" for line in lines))

    def flush(self) -> None:
        self._real.flush()

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._real.fileno()


class AgentEvents(headless.JsonEvents):
    """One agent's event stream, every line stamped with its name, sharing the
    run's lock. A swarm streams many replies at once, so text and reasoning
    deltas are flushed with the next event or within `headless.DELTA_FLUSH_S`."""

    coalesce_deltas = True

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


def _agent_config(spec: AgentSpec, *, save_session: bool = True) -> Any:
    """The config one agent's run starts from: its opts, its agent, its
    directory and its session. ``save_session`` off makes no session file."""
    extras = tuple(f"{k}={v}" for k, v in spec.opts.items())
    cwd = Path(spec.cwd).expanduser() if spec.cwd else Path.cwd()
    return from_env(save_session=save_session, sources=InvocationSources(extras=extras), agent_id=spec.agent,
                    session=spec.session, cwd=cwd)


def _configure(spec: AgentSpec) -> run_assembly.SettledRun:
    """The settled run of one agent: the bus tools on its surface and its
    seat file after its system prompt."""
    cfg = _agent_config(spec)
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
    return replace(settled, system=system, registry=bus_tools.with_bus_tools(settled.registry).with_public_aliases(
        settled.prompt_spec.tool_aliases, strict=True,
    ))


@contextlib.contextmanager
def _recruiting(root: Path):
    """Hold the bus's recruit lock: one recruit at a time over every runner
    process and tool thread on ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".recruit.lock", "a", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        yield


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
        # The jsrc lines whose `on` handler could not register, reported once
        # for the run however many agents read them.
        self.reported_hooks: set = set()
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
        stderr, sys.stderr = sys.stderr, AgentStderr(sys.stderr, self.root)
        try:
            while self.tasks:
                await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
                for name in [n for n, t in self.tasks.items() if t.done()]:
                    self.tasks.pop(name, None)
        finally:
            watcher.cancel()
            sys.stderr = stderr
            await model_client.close_shared_http_clients()
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
            # The name and the cap are checked and the marker written under
            # the bus's recruit lock, which every runner on the bus takes.
            with _recruiting(self.root):
                if name in agent.room.members() or name in self.tasks or (home / "spawned").is_file():
                    raise ValueError(f"{name} is already on the bus")
                spawned = sum(1 for p in self.root.iterdir() if (p / "spawned").is_file())
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
        _AGENT.set(spec.name)
        events = AgentEvents(self.out, self.lock, spec.name)
        reason = "stopped"
        chat = None
        context = ToolContext(cwd=Path(spec.cwd).expanduser() if spec.cwd else Path.cwd())
        context.net_label = spec.name
        # The agent's session is its own while it runs a turn or sleeps on it,
        # as a subagent's is: a second runner on it fails instead of writing.
        held = contextlib.ExitStack()
        try:
            agent = swarm.Agent(self.root / spec.name)
            agent.recruiter = self.recruiter(spec, agent)
            context.swarm = agent
            configure = _configure if self.configure is None else self.configure
            settled = configure(spec)
            cfg, system = settled.config, settled.system
            held.enter_context(subagents.worker_session(Path(cfg.session_file)))
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
            # The jsrc `on` handlers whose command stands alone, as `-p` runs
            # them: an `on tool_call` guard vets this agent's calls, and what
            # a handler prints goes to stderr, never to the event stream.
            hooks = standalone_hooks.table(
                cfg, jsrc_paths(Path(getattr(cfg, "project_dir", None) or context.cwd)),
                reported=self.reported_hooks)
            turn_kwargs = settled.turn_kwargs(suppress_output=True, trace_override=False, steer=agent.steer,
                                              event_sink=events.runtime_event, event_hooks=hooks)
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
            held.close()
            subagents.close_child_tools(context)
            if isinstance(sys.stderr, AgentStderr):
                sys.stderr.finish(spec.name)
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


# Agents per runner process when SWARM_PROCESSES is 0.
AGENTS_PER_PROCESS = 16


def processes_for(agents: int, setting: Any, slots: int = 0) -> int:
    """How many runner processes a swarm of ``agents`` is spread over:
    SWARM_PROCESSES, or with 0 one per AGENTS_PER_PROCESS agents up to a
    process per CPU; never more than one per agent, nor than ``slots`` when
    the swarm has a cap, so each process holds a slot of it."""
    try:
        wanted = max(0, int(setting))
    except (TypeError, ValueError):
        wanted = 0
    if wanted == 0:
        wanted = min(os.cpu_count() or 1, -(-agents // AGENTS_PER_PROCESS))
    if slots > 0:
        wanted = min(wanted, slots)
    return max(1, min(agents, wanted))


# Seconds a runner whose launch is abandoned has to end before it is killed.
RUNNER_END_WAIT = 5.0


def _first_agent_slots(raw: dict) -> int:
    """The cap a spec with no ``slots`` runs under: SWARM_ACTIVE_MAX in its
    first agent's config, which is the cap one runner takes; the live
    setting when that config cannot be read. Settling a run leaves that
    setting as the config has it, so the agent's prompt is not loaded here."""
    try:
        # Without its session: one the run has yet to make is not there to read.
        first = replace(AgentSpec.from_dict((raw.get("agents") or [{}])[0]), session=None)
        return _slots(_agent_config(first, save_session=False), None)
    except Exception:  # noqa: BLE001 - the agent's own runner reports what is wrong with it
        return _slots(None, _settings.live_value("SWARM_ACTIVE_MAX") or 0)


def _shard_specs(raw: dict, count: int) -> list[dict]:
    """The spec split into ``count`` specs that each run in one process:
    the agents dealt out in turn, and a ``slots`` cap dealt out the same way."""
    agents = list(raw.get("agents") or [])
    slots = int(raw.get("slots") or 0)
    specs = []
    for i in range(count):
        spec = {**raw, "agents": agents[i::count], "processes": 1}
        if slots > 0:
            spec["slots"] = slots // count + (1 if i < slots % count else 0)
        specs.append(spec)
    return specs


def _is_delta(line: str) -> bool:
    return line.endswith(('"type": "text"}\n', '"type": "reasoning"}\n'))


def run_sharded(path: str | os.PathLike[str], count: int, out: TextIO | None = None, *,
                raw: dict | None = None) -> int:
    """Run the spec's agents over ``count`` runner processes, each one
    ``python -m js.swarm run`` on its share. Their events come out on this
    process's stdout as they are written, and one ``run_end`` for the whole
    swarm; their stderr is this process's. SIGINT and SIGTERM go to every
    runner. The exit code is 130 when any runner was interrupted, else the
    highest a runner returned. ``raw`` is the spec to split; None reads it
    from ``path``."""
    import subprocess

    out = out or sys.stdout
    if raw is None:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    root, _agents, _slots = load_spec(path)
    shards = root / ".runners"
    shards.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    # The spec's agents and every one a runner started besides (a recruit).
    names: list[str] = [str(a.get("name")) for a in raw.get("agents") or []]
    children: list[subprocess.Popen] = []
    written: list[Path] = []
    try:
        for i, spec in enumerate(_shard_specs(raw, count)):
            shard = shards / f"{os.getpid()}-{i}.json"
            written.append(shard)
            shard.write_text(json.dumps(spec), encoding="utf-8")
            children.append(subprocess.Popen([sys.executable, "-m", "js.swarm", "run", str(shard)],
                                             stdout=subprocess.PIPE, text=True, encoding="utf-8"))
    except BaseException:
        # A runner that could not start ends the ones already started. Their
        # stdout is closed first, so one blocked writing to it is not waited on.
        for child in children:
            child.stdout.close()
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=RUNNER_END_WAIT)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for shard in written:
            shard.unlink(missing_ok=True)
        raise

    flushed = [0.0]

    def relay(child: subprocess.Popen) -> None:
        for line in child.stdout:
            # Only the runner's own events are read; the rest pass through.
            event = None
            if '"run_end"' in line or '"agent_start"' in line:
                with contextlib.suppress(ValueError):
                    event = json.loads(line)
            if isinstance(event, dict):
                if event.get("type") == "run_end":
                    continue
                if event.get("type") == "agent_start":
                    names.append(str(event.get("agent")))
            with lock:
                out.write(line)
                now = time.monotonic()
                if not _is_delta(line) or now - flushed[0] >= headless.DELTA_FLUSH_S:
                    out.flush()
                    flushed[0] = now

    readers = [threading.Thread(target=relay, args=(child,), daemon=True) for child in children]
    for reader in readers:
        reader.start()

    def forward(signum: int, _frame: Any) -> None:
        for child in children:
            with contextlib.suppress(OSError):
                child.send_signal(signum)

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        codes = [child.wait() for child in children]
        for reader in readers:
            reader.join()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        for child in children:
            with contextlib.suppress(OSError):
                Path(child.args[-1]).unlink()
    code = 130 if 130 in codes or any(c < 0 for c in codes) else max(codes)
    AgentEvents(out, lock, "").emit("run_end", agents=list(dict.fromkeys(names)), exit_code=code)
    return code


def main(path: str) -> int:
    try:
        paths.configure_home()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        agents = len(raw.get("agents") or [])
        processes = raw["processes"] if "processes" in raw else _settings.live_value("SWARM_PROCESSES")
        slots = raw.get("slots")
        if slots is None and processes_for(agents, processes) > 1:
            # One cap for the whole swarm, dealt out over its runners.
            slots = _first_agent_slots(raw)
        count = processes_for(agents, processes, int(slots or 0))
        if count > 1:
            return run_sharded(path, count, raw={**raw, "slots": int(slots or 0)})
        return run(path)
    except (OSError, ValueError) as exc:
        print(msgs.SWARM_RUN_FAILED.format(exc=exc), file=sys.stderr)
        return 2
