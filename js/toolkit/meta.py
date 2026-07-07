"""Meta tools: todos, followup, plans, skills, and isolated task delegation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import asyncio
import json
import os
import secrets
import time
from typing import Any

from .core import Todo, Tool, ToolContext
from .descriptions import load_description
from .sanitize import int_or_default


_ALLOWED_STATUS = {"pending", "in_progress", "completed", "cancelled"}
_DEFAULT_TASK_DEPTH = 2
# Concurrency ceiling for a single fan-out. The parent tool-dispatch pool is 32
# threads (cli.py); a sub-fan-out sits well below that so one wide `task(...)`
# — or a nested one two levels deep — can't storm the provider with dozens of
# simultaneous calls. Kept a module default (not a settings knob) to stay inside
# the meta/registry surface; `context.subagent_max_workers` overrides it when a
# future knob threads one through.
_DEFAULT_SUBAGENT_MAX_WORKERS = 8


def todo_write(todos: list[dict], context: ToolContext | None = None) -> str:
    assert context is not None
    before = [(todo.content, todo.status) for todo in context.todos.values()]
    for item in todos:
        content = str(item.get("content", "")).strip()
        status = str(item.get("status", "pending")).strip().lower()
        if not content:
            return "ERROR: Todo content cannot be empty"
        if status not in _ALLOWED_STATUS:
            return f"ERROR: invalid todo status {status!r}; use pending, in_progress, completed, or cancelled"
        if status == "cancelled":
            context.todos.pop(content, None)
        else:
            context.todos[content] = Todo(content=content, status=status)
    after = [(todo.content, todo.status) for todo in context.todos.values()]
    return f"todos updated\nbefore={before}\nafter={after}"


def todo_read(context: ToolContext | None = None) -> str:
    assert context is not None
    if not context.todos:
        return "No todos."
    return "\n".join(f"- [{todo.status}] {todo.content}" for todo in context.todos.values())


def followup(
    question: str,
    multiple: bool | None = False,
    option1: str | None = None,
    option2: str | None = None,
    option3: str | None = None,
    option4: str | None = None,
    option5: str | None = None,
    context: ToolContext | None = None,
) -> str:
    options = [opt for opt in (option1, option2, option3, option4, option5) if opt]
    lines = ["FOLLOWUP_REQUIRED", question]
    if options:
        choice_kind = "select one or more" if multiple else "select one"
        lines.append(choice_kind + ":")
        lines.extend(f"{idx}. {opt}" for idx, opt in enumerate(options, 1))
    return "\n".join(lines)


def plan(plan_name: str, version: str, content: str, context: ToolContext | None = None) -> str:
    assert context is not None
    safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in plan_name).strip("-.") or "plan"
    safe_version = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in version).strip("-.") or "v1"
    target = context.resolve_path(Path("plans") / f"{safe_name}-{safe_version}.md")
    context.snapshot(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"plan written to {target}"


def skill(name: str, context: ToolContext | None = None) -> str:
    assert context is not None
    pkg_root = Path(__file__).resolve().parents[1]
    candidates = [
        pkg_root / "skills" / f"{name}.md",
        pkg_root / "skills" / name / "SKILL.md",
        context.resolve_path(Path("skills") / f"{name}.md"),
        context.resolve_path(Path("skills") / name / "README.md"),
        context.resolve_path(Path(".skills") / f"{name}.md"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(errors="replace")
    return f"ERROR: skill {name!r} not found in js/skills or local skills directories"


def _task_text(item: Any) -> str:
    return str(item).strip()



def _task_system(agent: str, session_id: str | None) -> str:
    session = f" session_id={session_id}" if session_id else ""
    return (
        f"You are a fresh non-interactive worker agent. agent_id={agent}{session}.\n"
        "Complete exactly the assigned task using the available tools. "
        "Keep your final answer compressed: outcome, changed files if any, commands run, blockers. "
        "Do not ask follow-up questions unless the task is impossible without operator input."
    )


def _child_context(parent: ToolContext, registry: Any, agent: str) -> ToolContext:
    child = ToolContext(
        cwd=parent.cwd,
        max_read_lines=parent.max_read_lines,
        max_line_chars=parent.max_line_chars,
        max_file_bytes=parent.max_file_bytes,
        max_tool_result_bytes=parent.max_tool_result_bytes,
        max_bash_output_bytes=parent.max_bash_output_bytes,
        fetch_timeout_s=parent.fetch_timeout_s,
        task_max_depth=getattr(parent, "task_max_depth", _DEFAULT_TASK_DEPTH),
        wiki_vault_lock_timeout_s=getattr(parent, "wiki_vault_lock_timeout_s", 30),
        wiki_mode=getattr(parent, "wiki_mode", ""),
        wiki_no_archive=getattr(parent, "wiki_no_archive", False),
        artifact_dir=getattr(parent, "artifact_dir", None),
        artifact_url=getattr(parent, "artifact_url", None),
        artifact_bin=getattr(parent, "artifact_bin", None),
        vault_aliases=getattr(parent, "vault_aliases", {}) or {},
    )
    child.tool_registry = registry
    child.agent_id = agent
    child.task_depth = getattr(parent, "task_depth", 0) + 1
    return child


def _write_latest(agent_dir: Path, session_file: Path) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    tmp = agent_dir / f".latest.{secrets.token_hex(6)}.tmp"
    tmp.write_text(
        json.dumps({"session_file": str(session_file), "session_name": session_file.name}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, agent_dir / "latest.json")


def _session_file_for_id(agent_dir: Path, sessions_dir: Path, session_id: str) -> Path:
    raw = Path(session_id).expanduser()
    if raw.is_absolute():
        resolved = raw.resolve(strict=False)
        if resolved.suffix != ".jsonl" or not resolved.is_relative_to(sessions_dir.resolve(strict=False)):
            raise ValueError(f"session path must be a .jsonl file inside {sessions_dir}: {session_id}")
        path = raw
    else:
        path = sessions_dir / (raw if raw.suffix else raw.with_suffix(".jsonl"))
        if path.suffix != ".jsonl":
            raise ValueError(f"session_id must resolve to a .jsonl file: {session_id}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    _write_latest(agent_dir, path)
    return path


def _reserve_worker_session(agent_dir: Path, sessions_dir: Path) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        session_file = sessions_dir / f"task-{int(time.time() * 1000)}-{secrets.token_hex(8)}.jsonl"
        try:
            with session_file.open("x", encoding="utf-8"):
                pass
        except FileExistsError:
            continue
        _write_latest(agent_dir, session_file)
        return session_file
    raise RuntimeError(f"could not reserve task session under {sessions_dir}")



def _select_agent_prompt_dir(agent: str, prompt_roots: tuple[Path, ...]) -> Path:
    """Select the most-specific prompt dir from repo/global/project roots."""
    for root in reversed(prompt_roots):
        candidate = root / agent
        if candidate.is_dir() and any(candidate.glob("*.md")):
            return candidate
    return (prompt_roots[0] if prompt_roots else Path("prompts")) / agent

def _agent_cfg(parent_cfg: Any, agent: str, session_id: str | None) -> Any:
    # The per-agent data dir is the sessions dir; it stores that agent's jsonl,
    # .history, and latest.json.
    agents_root = parent_cfg.agent_dir.parent
    prompt_roots = tuple(getattr(parent_cfg, "prompt_roots", ()) or (parent_cfg.prompts_dir.parent,))
    agent_dir = agents_root / agent
    sessions_dir = agent_dir
    session_file = _session_file_for_id(agent_dir, sessions_dir, session_id) if session_id else _reserve_worker_session(agent_dir, sessions_dir)
    return replace(
        parent_cfg,
        agent_id=agent,
        agent_dir=agent_dir,
        history_file=agent_dir / ".history",
        sessions_dir=sessions_dir,
        session_file=session_file,
        prompts_dir=_select_agent_prompt_dir(agent, prompt_roots),
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
) -> str:
    from .. import memory as M
    from .. import persona as P
    from ..runtime import Telemetry, run_turn_async
    from ..sampling import Sampling
    from .. import routing

    prompt = _task_text(item)
    if not prompt:
        return f"{idx}. ERROR task is empty"

    agent = agent_id
    task_session_id = global_session_id
    cfg = _agent_cfg(parent_cfg, agent, task_session_id)

    try:
        prompt_spec = P.load_prompt_spec(cfg.prompts_dir)
        if getattr(cfg, "agents_files", ()):
            parts = [
                p.read_text(encoding="utf-8").rstrip()
                for p in cfg.agents_files
                if p.is_file() and p.read_text(encoding="utf-8").strip()
            ]
            if parts:
                system = "\n\n".join([*parts, prompt_spec.system.rstrip()])
                system = system.rstrip() + "\n"
                prompt_spec = P.PromptSpec(
                    system=system,
                    tool_selectors=prompt_spec.tool_selectors,
                    sampling=prompt_spec.sampling,
                    model=prompt_spec.model,
                    secondary_model=prompt_spec.secondary_model,
                )
    except FileNotFoundError:
        prompt_spec = P.PromptSpec(system="", tool_selectors=())
    except ValueError as exc:
        return f"{idx}. ERROR could not load agent {agent!r}: {exc}"

    # Subagent model precedence (operator-locked order):
    #   tool-call model (main agent wins, unless lock_subagent_model) >
    #   inherit parent (if prefer_inherit) > frontmatter primary (`model:`) >
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
    # FUTURE (stub): a non-config runtime flag — not yet built — will select the
    # agent's secondary model here via prompt_spec.secondary_model. No flag wired
    # yet, so this stays a no-op.
    use_secondary = False
    if use_secondary and getattr(prompt_spec, "secondary_model", ""):
        chosen = prompt_spec.secondary_model
    if chosen and chosen != cfg.model:
        route = routing.resolve_model_route(
            chosen,
            configured_provider_id=cfg.provider_id,
            configured_base_url=cfg.provider_base_url,
            configured_api_key=cfg.provider_api_key,
            configured_headers=getattr(cfg, "provider_headers", {}),
            explicit_model=True,
            prefix_overrides_provider=not getattr(cfg, "explicit_provider", False),
        )
        cfg = replace(
            cfg,
            model=route.model,
            provider_id=route.provider_id,
            provider_base_url=route.base_url,
            provider_api_key=route.api_key,
            provider_headers=route.headers,
        )

    registry = full_registry.select(prompt_spec.tool_selectors, agent_id=agent)
    system = prompt_spec.system + "\n" + _task_system(agent, task_session_id)
    sampling = (
        cfg.sampling_setscript
        .merge(Sampling.from_mapping(prompt_spec.sampling))
        .merge(cfg.sampling_env)
        .merge(cfg.sampling_cli)
    )

    child_context = _child_context(parent_context, registry, agent)
    child_context.config = cfg
    messages = M.load_messages(cfg.session_file)
    before_len = len(messages)
    messages.append({"role": "user", "content": prompt})
    try:
        await run_turn_async(
            cfg,
            system,
            messages,
            Telemetry(debug_log=cfg.debug_log),
            trace_override=False,
            tool_registry=registry,
            tool_context=child_context,
            suppress_output=True,
            sampling=sampling,
        )
    except Exception as exc:  # noqa: BLE001
        messages[:] = messages[:before_len]
        return f"{idx}. ERROR {type(exc).__name__}: {exc}"

    for new_message in messages[before_len:]:
        M.append_message(cfg.session_file, new_message)
    final = ""
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            final = str(msg["content"]).strip()
            break
    final = final or "(no final response)"
    # Give each child a FAIR SHARE of the aggregate budget, not the whole thing.
    # The parent's dispatch layer re-clips the joined TASK_RESULTS at
    # max_tool_result_bytes; if every child could fill that full budget, one fat
    # sibling would fill it alone and the aggregate re-clip would slice the rest
    # away — the short siblings vanish. Capping each at budget//total bounds a fat
    # sibling to its 1/N slice, so the short ones always survive the join. (A run
    # where ALL N are fat still trims the tail at the aggregate cap — inherent to a
    # fixed budget, not silent starvation.) Read the parent context: the child's
    # runtime rewrites child_context.max_tool_result_bytes mid-turn.
    budget = int(getattr(parent_context, "max_tool_result_bytes", 0) or 0)
    cap = budget // max(1, total) if budget else 0
    if cap and len(final) > cap:
        final = final[:cap] + f"\n[truncated: limits.max_tool_result_bytes ({cap}) reached]"
    return f"{idx}. {final}"


def _fan_out(indexed_items: list[tuple[int, Any]], coro_factory) -> list[str | None]:
    """Run each subagent turn concurrently, results returned in task order. Two
    ramps, chosen by whether a non-blocking REPL supervisor is live:

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
            except Exception as exc:  # noqa: BLE001
                results[idx - 1] = f"{idx}. ERROR {type(exc).__name__}: {exc}"
        return results

    async def _gather():
        return await asyncio.gather(
            *(coro_factory(idx, item) for idx, item in indexed_items),
            return_exceptions=True,
        )

    for (idx, _item), res in zip(indexed_items, asyncio.run(_gather())):
        results[idx - 1] = (
            f"{idx}. ERROR {type(res).__name__}: {res}" if isinstance(res, Exception) else res
        )
    return results


async def _fan_out_async(indexed_items: list[tuple[int, Any]], coro_factory) -> list[str | None]:
    """On-loop sibling of `_fan_out` for the non-blocking REPL. The CALLER is
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
            results[idx - 1] = (
                f"{idx}. ERROR {type(res).__name__}: {res}"
                if isinstance(res, BaseException)
                else res
            )
        return results

    gathered = await asyncio.gather(
        *(coro_factory(idx, item) for idx, item in indexed_items),
        return_exceptions=True,
    )
    for (idx, _item), res in zip(indexed_items, gathered):
        results[idx - 1] = (
            f"{idx}. ERROR {type(res).__name__}: {res}"
            if isinstance(res, BaseException)
            else res
        )
    return results


def _subagent_worker_cap(context: ToolContext, total: int) -> int:
    """Concurrency ceiling for one fan-out: at most `total` workers, never above
    the configured/module width limit."""
    limit = int_or_default(
        getattr(context, "subagent_max_workers", _DEFAULT_SUBAGENT_MAX_WORKERS),
        _DEFAULT_SUBAGENT_MAX_WORKERS,
        minimum=1,
    )
    return max(1, min(total, limit))


def _prepare_fan_out(
    tasks: list[Any],
    agent_id: str,
    session_id: str | None,
    model: str,
    context: ToolContext,
) -> tuple[str, None] | tuple[None, tuple[list[tuple[int, Any]], Any]]:
    """Validate a task/named-agent call and build (indexed_items, coro_factory).

    Returns ``(error_message, None)`` on a validation/depth failure, else
    ``(None, (indexed_items, coro_factory))``. Shared verbatim by the sync
    ``task`` (``-p``/bench) and async ``task_async`` (non-blocking REPL) paths so
    the two rails can never drift. Each coroutine is gated by a shared semaphore
    so a wide (or nested) fan-out never runs more than the worker cap at once."""
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

    max_depth = int_or_default(getattr(context, "task_max_depth", _DEFAULT_TASK_DEPTH), _DEFAULT_TASK_DEPTH, minimum=1)
    if getattr(context, "task_depth", 0) >= max_depth:
        return f"ERROR: task recursion depth limit reached ({max_depth})", None

    from ..config import from_env
    from .registry import build_default_registry

    parent_cfg = getattr(context, "config", None) or from_env(save_session=True)
    # Honor the subagent-model lock at every depth: when locked, rebuild the
    # nested registry without the `model_override` flag so the task tool's
    # `model` param is gone from the schema/description, not just ignored
    # (mirrors cli._registry_for).
    reg_flags = () if getattr(parent_cfg, "lock_subagent_model", False) else ("model_override",)
    full_registry = build_default_registry(getattr(parent_cfg, "prompt_roots", None), flags=reg_flags)
    total = len(normalized_items)
    gate = asyncio.Semaphore(_subagent_worker_cap(context, total))

    def _coro(idx: int, item: Any):
        async def _gated() -> str:
            async with gate:
                return await _run_one_task_async(
                    idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model
                )

        return _gated()

    indexed_items = [(idx, item) for idx, item in enumerate(normalized_items, 1)]
    return None, (indexed_items, _coro)


def _assemble_task_results(results: list[str | None], agent_id: str, session_id: str | None) -> str:
    filled = [result if result is not None else f"{idx}. ERROR worker did not return" for idx, result in enumerate(results, 1)]
    header = f"TASK_RESULTS agent={agent_id}"
    if session_id:
        header += f" session_id={session_id}"
    return "\n\n".join([header, *filled])


def task(
    tasks: list[Any],
    agent_id: str = "",
    session_id: str | None = None,
    model: str = "",
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    agent_id = str(agent_id).strip()
    model = str(model or "").strip()
    error, prepared = _prepare_fan_out(tasks, agent_id, session_id, model, context)
    if prepared is None:
        return error  # type: ignore[return-value]
    indexed_items, coro_factory = prepared
    results = _fan_out(indexed_items, coro_factory)
    return _assemble_task_results(results, agent_id, session_id)


task._js_fan_out = True  # type: ignore[attr-defined]


async def task_async(
    tasks: list[Any],
    agent_id: str = "",
    session_id: str | None = None,
    model: str = "",
    context: ToolContext | None = None,
) -> str:
    """Async twin of :func:`task` for the non-blocking REPL: awaits child turns
    on the caller's loop via :func:`_fan_out_async` instead of blocking a
    dispatch thread. The runtime routes fan-out calls here when a supervisor is
    live (see ``runtime._dispatch_batch``)."""
    assert context is not None
    agent_id = str(agent_id).strip()
    model = str(model or "").strip()
    error, prepared = _prepare_fan_out(tasks, agent_id, session_id, model, context)
    if prepared is None:
        return error  # type: ignore[return-value]
    indexed_items, coro_factory = prepared
    results = await _fan_out_async(indexed_items, coro_factory)
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
    if agent_id is None:
        agent_id = str(args.get("agent_id", "")).strip()
    return await task_async(
        tasks=args.get("tasks"),
        agent_id=agent_id,
        session_id=args.get("session_id"),
        model=args.get("model", ""),
        context=context,
    )


def named_agent_tool(agent_id: str, description: str | None = None) -> Tool:
    def run_named_agent(tasks: list[Any], context: ToolContext | None = None) -> str:
        return task(tasks=tasks, agent_id=agent_id, context=context)

    # Tag so the runtime recognizes this as fan-out and awaits it on-loop under a
    # supervisor (like the `task` tool); `_js_agent_id` carries the fixed worker.
    run_named_agent._js_fan_out = True  # type: ignore[attr-defined]
    run_named_agent._js_agent_id = agent_id  # type: ignore[attr-defined]

    text = description or (
        f"Run the `{agent_id}` agent as a subagent. Provide one or more task strings; "
        "the agent loads its own persona and selected tool surface."
    )
    return Tool(
        agent_id,
        text,
        run_named_agent,
        {"tasks": {"type": "array", "items": {"type": "string"}, "description": "One or more clear, detailed task prompts to run in parallel."}},
        required=("tasks",),
    )


def _task_params(flags: tuple[str, ...]) -> dict:
    params = {
        "tasks": {"type": "array", "items": {"type": "string"}, "description": "One or more clear, detailed task prompts to run in parallel."},
        "agent_id": {"type": "string", "description": "Worker agent id; loads that agent's persona and selected tools."},
        "session_id": {"type": "string", "description": "Optional session id for a worker context."},
    }
    if "model_override" in flags:
        params["model"] = {"type": "string", "description": "Model for the subagent(s), same string as --model. Set it when the operator names one; omit otherwise."}
    return params


def tools(flags: tuple[str, ...] = ("model_override",)) -> tuple[Tool, ...]:
    return (
        Tool(
            "todo_write",
            load_description("todo_write"),
            todo_write,
            {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": sorted(_ALLOWED_STATUS)}}}}},
            required=("todos",),
        ),
        Tool("todo_read", load_description("todo_read"), todo_read, {}),
        Tool(
            "followup",
            load_description("followup"),
            followup,
            {"question": {"type": "string"}, "multiple": {"type": "boolean"}, "option1": {"type": "string"}, "option2": {"type": "string"}, "option3": {"type": "string"}, "option4": {"type": "string"}, "option5": {"type": "string"}},
            required=("question",),
        ),
        Tool("plan", load_description("plan"), plan, {"plan_name": {"type": "string", "description": "Plan name used in the filename."}, "version": {"type": "string", "description": "Version suffix used in the filename."}, "content": {"type": "string", "description": "Markdown plan body to persist."}}, required=("plan_name", "version", "content")),
        Tool("skill", load_description("skill"), skill, {"name": {"type": "string", "description": "Local skill name to load."}}, required=("name",)),
        Tool(
            "task",
            load_description("task", flags=flags),
            task,
            _task_params(flags),
            required=("tasks", "agent_id"),
        ),
    )
