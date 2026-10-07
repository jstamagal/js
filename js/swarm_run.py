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

Events go to stdout as JSON lines, every one carrying ``agent``; ``agent_start``,
``agent_end`` and ``run_end`` are the runner's own. The process ends when every
agent has stopped or retired. SIGTERM and SIGINT end it like ^C: every session
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

from . import agent_messages, compaction, headless, runtime
from . import memory as M
from . import persona as P
from . import swarm
from .config import from_env
from .session_catalog import record_session_start
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
    raw = settings.get("swarm", {}).get("slots") if isinstance(settings.get("swarm"), dict) else None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _configure(spec: AgentSpec) -> tuple[Any, str, Any, Any]:
    """The config, system prompt, prompt spec and tool registry of one agent."""
    from . import cli

    extras = [f"{k}={v}" for k, v in spec.opts.items()]
    cwd = Path(spec.cwd).expanduser() if spec.cwd else Path.cwd()
    cfg = from_env(save_session=True, extras=extras, agent_id=spec.agent, session=spec.session, cwd=cwd)
    prompt_spec = P.load_configured_prompt_spec(cfg, expand=False)
    cfg = cli._apply_agent_model(cfg, prompt_spec, spec.model, config_pins=False)
    if spec.effort is None:
        cfg = cli._apply_agent_reasoning(cfg, prompt_spec)
    else:
        effort, error = cli._validate_cli_reasoning(spec.effort)
        if error:
            raise ValueError(error)
        cfg = replace(cfg, reasoning_effort=effort)
    cfg = P.apply_agent_max_tokens(cfg, prompt_spec)
    cfg = cli._resolve_cli_model_override(cfg, spec.model)
    prompt_spec = P._expand_spec(prompt_spec, cfg)
    system = prompt_spec.system
    if spec.seat_file:
        system = system.rstrip() + "\n\n" + Path(spec.seat_file).expanduser().read_text(encoding="utf-8")
    registry = swarm.with_bus_tools(cli._registry_for(cfg).select(prompt_spec.tool_selectors))
    return cfg, system, prompt_spec, registry


class Runner:
    def __init__(self, root: Path, specs: list[AgentSpec], slots: int | None, out: TextIO) -> None:
        self.root = root
        self.specs = specs
        self.spec_slots = slots
        self.out = out
        self.lock = threading.Lock()
        self.tasks: dict[str, asyncio.Task] = {}
        self.gate: Any = None
        self.code = 0

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.cancel_all)
        for spec in self.specs:
            self.add(spec)
        while self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
            for name in [n for n, t in self.tasks.items() if t.done()]:
                self.tasks.pop(name, None)
        AgentEvents(self.out, self.lock, "").emit("run_end", agents=[s.name for s in self.specs],
                                                   exit_code=self.code)
        return self.code

    def add(self, spec: AgentSpec) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(self.run_agent(spec), name=f"swarm:{spec.name}")
        self.tasks[spec.name] = task
        return task

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
            if spawned >= swarm.SPAWN_CAP:
                raise ValueError(f"this bus already has {spawned} recruited agents; the cap is {swarm.SPAWN_CAP}")
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
        agent = swarm.Agent(self.root / spec.name)
        agent.recruiter = self.recruiter(spec, agent)
        reason = "stopped"
        cfg = None
        messages: list[dict] = []
        context = ToolContext(cwd=Path(spec.cwd).expanduser() if spec.cwd else Path.cwd())
        context.swarm = agent
        context.net_label = spec.name
        try:
            cfg, system, prompt_spec, registry = _configure(spec)
            if self.gate is None:
                slots = _slots(cfg, self.spec_slots)
                self.gate = asyncio.Semaphore(slots) if slots > 0 else contextlib.nullcontext()
            telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
            messages = M.load_replay_messages(cfg.session_file)
            if not messages and Path(cfg.session_file) != Path(os.devnull):
                record_session_start(cfg.session_file, cwd=context.cwd, agent=cfg.agent_id, model=cfg.model,
                                     mode="swarm")
            turn_kwargs: dict[str, Any] = {
                "model_override": cfg.model,
                "provider_id_override": cfg.provider_id,
                "provider_base_url_override": cfg.provider_base_url,
                "provider_api_key_override": cfg.provider_api_key,
                "tool_registry": registry,
                "tool_context": context,
                "suppress_output": True,
                "trace_override": False,
                "sampling": _sampling(cfg, prompt_spec),
                "steer": agent.steer,
                "event_sink": events.runtime_event,
            }
            stamp = M.stamp_for(cfg.model, cfg.provider_id, cfg.reasoning_effort)
            auto = compaction.AutoCompactState()
            events.emit("agent_start", model=cfg.model, provider=cfg.provider_id, session=str(cfg.session_file),
                        cwd=str(context.cwd))
            await agent_messages.initialize(cfg, prompt_spec, messages, telemetry, system=system, **turn_kwargs)
            messages.append(M.note_time({"role": "user", "content": spec.opener}))
            async with self.gate:
                await runtime.run_turn_async(cfg, system, messages, telemetry, **turn_kwargs)
            while not agent.stop_seen:
                M.persist_messages(cfg.session_file, messages, stamp=stamp)
                await self.compact(cfg, auto, context, system, messages)
                events.emit("sleep")
                landed = await agent.sleep_async()
                events.emit("wake", count=len(landed), seqs=[m.seq for m in landed if m.seq],
                            kinds=sorted({m.kind for m in landed}))
                if all(m.kind == swarm.STOP for m in landed):
                    break
                messages.append(M.note_time(agent.wake_message(landed)))
                async with self.gate:
                    await runtime.run_turn_async(cfg, system, messages, telemetry, **turn_kwargs)
            reason = "retired" if agent.retired else "stopped"
        except asyncio.CancelledError:
            reason = "cancelled"
        except Exception as exc:  # noqa: BLE001 - one agent's failure ends that agent, not the swarm
            reason = "error"
            events.emit("error", message=f"{type(exc).__name__}: {exc}", retryable=False)
        finally:
            if cfg is not None:
                with contextlib.suppress(Exception):
                    M.persist_messages(cfg.session_file, messages,
                                       stamp=M.stamp_for(cfg.model, cfg.provider_id, cfg.reasoning_effort))
            close_terminal_sessions(context)
            events.emit("agent_end", reason=reason)

    @staticmethod
    async def compact(cfg: Any, auto: compaction.AutoCompactState, context: ToolContext, system: str,
                      messages: list[dict]) -> None:
        if not compaction.get_bool(cfg, "auto"):
            return
        with contextlib.suppress(Exception):
            await compaction.maybe_auto_compact_async(
                cfg, auto, context, system, messages,
                lambda: runtime._resolve_context_window(cfg.model, cfg.provider_id, cfg.provider_base_url),
            )


def _sampling(cfg: Any, prompt_spec: Any):
    from .sampling import Sampling

    return (cfg.sampling_setscript
            .merge(Sampling.from_mapping(getattr(prompt_spec, "sampling", {}) or {}))
            .merge(cfg.sampling_env)
            .merge(cfg.sampling_cli))


def load_spec(path: str | os.PathLike[str]) -> tuple[Path, list[AgentSpec], int | None]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    root = Path(str(raw.get("root") or "")).expanduser()
    if not str(raw.get("root") or ""):
        raise ValueError("the spec needs a root: the bus directory")
    agents = [AgentSpec.from_dict(d) for d in raw.get("agents") or []]
    if not agents:
        raise ValueError("the spec names no agents")
    names = [a.name for a in agents]
    if len(set(names)) != len(names):
        raise ValueError(f"agent names repeat: {', '.join(n for n in names if names.count(n) > 1)}")
    slots = raw.get("slots")
    return root, agents, None if slots is None else int(slots)


def run(path: str | os.PathLike[str], out: TextIO | None = None) -> int:
    root, agents, slots = load_spec(path)
    runner = Runner(root, agents, slots, out or sys.stdout)
    return asyncio.run(runner.run())


def main(path: str) -> int:
    try:
        return run(path)
    except (OSError, ValueError) as exc:
        print(f"swarm run: {exc}", file=sys.stderr)
        return 2
