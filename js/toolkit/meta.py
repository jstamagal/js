"""Meta tools: todos, followup, plans, skills, and isolated task delegation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
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
    prompts_root = prompt_roots[-1] if prompt_roots else parent_cfg.prompts_dir.parent
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


def _run_one_task(
    idx: int,
    total: int,
    item: Any,
    parent_context: ToolContext,
    parent_cfg: Any,
    full_registry: Any,
    agent_id: str,
    global_session_id: str | None,
) -> str:
    from .. import memory as M
    from .. import persona as P
    from ..runtime import Telemetry, run_turn

    prompt = _task_text(item)
    if not prompt:
        return f"{idx}. ERROR task is empty"

    agent = agent_id
    task_session_id = global_session_id
    cfg = _agent_cfg(parent_cfg, agent, task_session_id)

    try:
        prompt_spec = P.load_prompt_spec(cfg.prompts_dir)
        if getattr(cfg, "agents_files", ()):  # global then project AGENTS stack applies to subagents too
            parts = [p.read_text(encoding="utf-8").rstrip() for p in cfg.agents_files if p.is_file() and p.read_text(encoding="utf-8").strip()]
            if parts:
                prompt_spec = P.PromptSpec(system="\n\n".join([*parts, prompt_spec.system.rstrip()]).rstrip() + "\n", tool_selectors=prompt_spec.tool_selectors)
    except FileNotFoundError:
        prompt_spec = P.PromptSpec(system="", tool_selectors=())
    except ValueError as exc:
        return f"{idx}. ERROR could not load agent {agent!r}: {exc}"
    registry = full_registry.select(prompt_spec.tool_selectors)
    system = prompt_spec.system + "\n" + _task_system(agent, task_session_id)

    child_context = _child_context(parent_context, registry, agent)
    child_context.config = cfg
    messages = M.load_messages(cfg.session_file)
    before_len = len(messages)
    messages.append({"role": "user", "content": prompt})
    try:
        run_turn(
            cfg,
            system,
            messages,
            Telemetry(debug_log=cfg.debug_log),
            trace_override=False,
            tool_registry=registry,
            tool_context=child_context,
            suppress_output=True,
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
    if len(final) > 4000:
        final = final[:4000] + "\n... [truncated]"
    return f"{idx}. {final}"


def task(
    tasks: list[Any],
    agent_id: str = "",
    session_id: str | None = None,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    agent_id = str(agent_id).strip()
    if not agent_id:
        return "ERROR: task requires agent_id"
    if not isinstance(tasks, list):
        return "ERROR: task tasks must be a list of strings"
    if any(not isinstance(item, str) for item in tasks):
        return "ERROR: task tasks must be strings"
    normalized_items = [item.strip() for item in tasks if item.strip()]
    if not normalized_items:
        return "ERROR: task requires at least one non-empty task"

    max_depth = int_or_default(getattr(context, "task_max_depth", _DEFAULT_TASK_DEPTH), _DEFAULT_TASK_DEPTH, minimum=1)
    if getattr(context, "task_depth", 0) >= max_depth:
        return f"ERROR: task recursion depth limit reached ({max_depth})"

    from ..config import from_env
    from .registry import build_default_registry

    parent_cfg = getattr(context, "config", None) or from_env(save_session=True)
    full_registry = build_default_registry(getattr(parent_cfg, "prompt_roots", None))
    worker_count = len(normalized_items)
    results: list[str | None] = [None] * len(normalized_items)
    futures = {}
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="js-task")
    try:
        for idx, item in enumerate(normalized_items, 1):
            future = executor.submit(
                _run_one_task,
                idx,
                len(normalized_items),
                item,
                context,
                parent_cfg,
                full_registry,
                agent_id,
                session_id,
            )
            futures[future] = idx

        for future, idx in futures.items():
            try:
                results[idx - 1] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[idx - 1] = f"{idx}. ERROR {type(exc).__name__}: {exc}"
    finally:
        executor.shutdown(wait=True)

    filled = [result if result is not None else f"{idx}. ERROR worker did not return" for idx, result in enumerate(results, 1)]
    header = f"TASK_RESULTS agent={agent_id}"
    if session_id:
        header += f" session_id={session_id}"
    return "\n\n".join([header, *filled])


def named_agent_tool(agent_id: str, description: str | None = None) -> Tool:
    def run_named_agent(tasks: list[Any], context: ToolContext | None = None) -> str:
        return task(tasks=tasks, agent_id=agent_id, context=context)

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


def tools() -> tuple[Tool, ...]:
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
            load_description("task"),
            task,
            {
                "tasks": {"type": "array", "items": {"type": "string"}, "description": "One or more clear, detailed task prompts to run in parallel."},
                "agent_id": {"type": "string", "description": "Worker agent id; loads that agent's persona and selected tools."},
                "session_id": {"type": "string", "description": "Optional session id for a worker context."},
            },
            required=("tasks", "agent_id"),
        ),
    )
