"""The harness bench's scenarios, each run in a process of its own by ``run.py``.

    python bench/harness/scenarios.py NAME      # prints one JSON object of metrics

Every metric is a time in milliseconds unless its name says otherwise
(``_us`` microseconds, ``_s`` seconds, ``_mb``). A scenario builds what it
needs under the JS_HOME ``run.py`` gave it and touches nothing else.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import statistics
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _timed(fn, n: int) -> float:
    """Median wall microseconds of one ``fn()`` over ``n`` calls, after one warm call."""
    fn()
    samples = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples) * 1e6


def _work() -> Path:
    work = Path(os.environ["JS_BENCH_WORK"])
    work.mkdir(parents=True, exist_ok=True)
    return work


def _quiet():
    """js's own stdout and stderr go nowhere; the metrics go to the real stdout."""
    real = sys.stdout
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    return real


def _cfg(session: str | None = None, cwd: Path | None = None):
    from js import run_assembly
    from js.config import InvocationSources, from_env

    cfg = from_env(save_session=session is not None, sources=InvocationSources(), session=session,
                   cwd=cwd or _work())
    return run_assembly.settle(cfg, model="fake")


# ------------------------------------------------------------------ config

def config() -> dict:
    """Reading the layered settings and settling a run, as every agent start does."""
    from js import run_assembly
    from js.config import InvocationSources, from_env

    cwd = _work()
    load = lambda: from_env(save_session=False, sources=InvocationSources(), cwd=cwd)  # noqa: E731
    cfg = load()
    return {
        "from_env": _timed(load, 20) / 1000,
        "settle": _timed(lambda: run_assembly.settle(cfg, model="fake"), 20) / 1000,
    }


# ------------------------------------------------------------------- tools

def tools() -> dict:
    """One call of each local tool through the dispatcher a turn uses
    (argument repair, trace, the tool, result cap, console and transcript
    render), and the same call to the handler alone."""
    from js import runtime, transcript as transcript_mod, turn_dispatch
    from js.toolkit import ToolContext
    from js.toolkit.core import call_tool

    work = _work()
    settled = _cfg()
    cfg, registry = settled.config, settled.registry
    small = work / "small.py"
    small.write_text("def f(x):\n    return x + 1\n" * 20)
    big = work / "big.py"
    big.write_text("".join(f"def f{i}(x):\n    return x + {i}  # line of code number {i}\n" for i in range(1500)))
    tree = work / "tree"
    for i in range(200):
        d = tree / f"pkg{i % 10}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"mod{i}.py").write_text(f"import os\n\ndef handler_{i}():\n    return {i}\n")
    target = work / "edit.py"
    target.write_text("alpha = 1\nbeta = 2\ngamma = 3\n" * 30)
    context = ToolContext(cwd=work, config=cfg)
    telemetry = runtime.Telemetry(debug_log=None)
    transcript_mod.sync_transcript_sink(cfg, getattr(cfg, "settings", {}) or {}, telemetry)
    flip = [0]

    def patch_args() -> dict:
        flip[0] += 1
        old, new = ("alpha = 1", "alpha = 9") if flip[0] % 2 else ("alpha = 9", "alpha = 1")
        return {"file_path": str(target), "old_string": old, "new_string": new, "replace_all": True}

    doomed = work / "doomed.txt"

    def remove_args() -> dict:
        doomed.write_text("gone\n")
        return {"path": str(doomed), "permanent": True}

    notebook = work / "book.ipynb"
    notebook.write_text(json.dumps({"cells": [{"cell_type": "code", "id": "c1", "metadata": {}, "outputs": [],
                                               "execution_count": None, "source": "x = 1\n"}],
                                    "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
    skills = sorted(p.name for p in (HERE.parent.parent / "defaults" / "skills").iterdir() if p.is_dir())
    calls = {
        "read_small": ("read", lambda: {"file_path": str(small)}, 200),
        "read_big": ("read", lambda: {"file_path": str(big)}, 50),
        "write": ("write", lambda: {"file_path": str(work / "out.txt"), "content": "hello\n" * 50}, 100),
        "patch": ("patch", patch_args, 100),
        "fs_search_files": ("fs_search", lambda: {"pattern": "mod1*.py", "path": str(tree), "output_mode": "files"}, 50),
        "fs_search_content": ("fs_search", lambda: {"pattern": "def handler_1", "path": str(tree),
                                                    "output_mode": "content"}, 50),
        "shell_true": ("shell", lambda: {"command": "true"}, 30),
        "shell_echo": ("shell", lambda: {"command": "echo hello"}, 30),
        "plan": ("plan", lambda: {"plan_name": "bench", "version": 1, "content": "# plan\n\n- one\n- two\n"}, 100),
        "remove": ("remove", remove_args, 50),
        "notebook_edit": ("notebook_edit", lambda: {"file_path": str(notebook), "cell_id": "c1",
                                                    "new_source": f"x = {flip[0]}\n"}, 50),
        "skill": ("skill", lambda: {"name": skills[0] if skills else ""}, 50),
        "fetch_local": ("fetch", lambda: {"url": os.environ["JS_BASE_URL"] + "/models", "raw": True}, 20),
    }
    for seen in (target, notebook):  # patch and notebook_edit want the file read
        call_tool(registry.resolve("read"), {"file_path": str(seen)}, context)
    out: dict = {}
    for label, (name, args, n) in calls.items():
        tool = registry.resolve(name)
        if tool is None:
            continue
        try:
            first = call_tool(tool, args(), context)
        except Exception as exc:  # noqa: BLE001 - a tool this build cannot call is left out
            out[f"{label}_error"] = f"{type(exc).__name__}: {exc}"[:120]
            continue
        if isinstance(first, str) and first.startswith("ERROR"):
            out[f"{label}_error"] = first[:120]
            continue
        out[f"{label}_handler_us"] = round(_timed(lambda: call_tool(tool, args(), context), n))  # noqa: B023
        out[f"{label}_dispatch_us"] = round(_timed(
            lambda: turn_dispatch._dispatch(name, json.dumps(args()), telemetry, 200_000, True,  # noqa: B023
                                            None, registry, context), n))
    return out


# ----------------------------------------------------------------- session

def session() -> dict:
    """Saving into, replaying and listing sessions."""
    from js import memory

    work = _work()
    f = work / "sessions" / "big.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    blob = "line of tool output with some code in it\n" * 100
    messages: list[dict] = [{"role": "user", "content": "go"}]
    saves = []
    for i in range(300):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "read", "arguments": "{\"file_path\": \"x\"}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": blob})
        start = time.perf_counter()
        memory.persist_messages(f, messages)
        saves.append(time.perf_counter() - start)
    out = {
        "save_300_rounds_total": sum(saves) * 1000,
        "save_last": saves[-1] * 1000,
        "replay_1_4mb": _timed(lambda: memory.load_replay_messages(f), 10) / 1000,
    }
    try:
        from js import session_query

        for i in range(200):
            g = f.parent / f"s{i:03d}.jsonl"
            memory.persist_messages(g, [{"role": "user", "content": f"question {i}"},
                                        {"role": "assistant", "content": f"answer {i}"}])
        lister = getattr(session_query, "list_sessions", None)
        if lister is not None:
            out["list_200_sessions"] = _timed(lambda: lister(f.parent), 5) / 1000
    except Exception as exc:  # noqa: BLE001
        out["list_error"] = f"{type(exc).__name__}: {exc}"[:120]
    return out


# ------------------------------------------------------------------ budget

def budget() -> dict:
    """What a model call spends before it sends anything, on a long history:
    the tool specs, the token estimate and the conversion to SDK messages."""
    from js import context_budget, model_client

    settled = _cfg()
    registry = settled.registry
    blob = "line of tool output with some code in it\n" * 100
    messages: list[dict] = [{"role": "system", "content": settled.system}, {"role": "user", "content": "go"}]
    for i in range(300):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "read", "arguments": "{\"file_path\": \"x\"}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": blob})
    specs = registry.openai_specs()
    out = {
        "openai_specs": _timed(registry.openai_specs, 50) / 1000,
        "specs_to_ai_tools": _timed(lambda: model_client.tool_specs_to_ai_tools(specs), 50) / 1000,
        "estimate_request_tokens_600_messages": _timed(
            lambda: context_budget.estimate_request_tokens(messages=messages, tools=specs), 10) / 1000,
    }
    state = context_budget.TokenState()
    usage = types.SimpleNamespace(input_tokens=400_000, output_tokens=50)

    def record() -> None:
        state.record_provider_usage(usage, message_count=len(messages), messages=messages,
                                    system=settled.system, tools=specs)

    out["record_usage_600_messages"] = _timed(record, 10) / 1000
    out["budget_status_600_messages"] = _timed(
        lambda: state.budget_status(system=settled.system, messages=messages, tools=specs,
                                    context_window=1_000_000), 10) / 1000
    return out


# -------------------------------------------------------------------- turn

def turn() -> dict:
    """One turn of 40 tool rounds and a 2000-token reply against the fake
    model, in this process: js's CPU for the whole turn, with the console
    drawing to a discarded stdout as `-p` does."""
    from js import runtime, transcript as transcript_mod
    from js.conversation import Conversation
    from js.toolkit import ToolContext

    work = _work()
    settled = _cfg(session="bench-turn")
    cfg = settled.config
    context = ToolContext(cwd=work, config=cfg)
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
    transcript_mod.sync_transcript_sink(cfg, getattr(cfg, "settings", {}) or {}, telemetry)
    chat = Conversation(cfg, settled.system, registry=settled.registry, context=context, telemetry=telemetry)
    kwargs = settled.turn_kwargs()

    async def go() -> None:
        await chat.turn({"role": "user", "content": "bench turn go"}, **kwargs)

    cpu, wall = time.process_time(), time.perf_counter()
    asyncio.run(go())
    cpu, wall = time.process_time() - cpu, time.perf_counter() - wall
    rounds = sum(1 for m in chat.messages if m.get("role") == "tool")
    return {"turn_cpu_s": cpu, "turn_wall_s": wall, "tool_rounds": rounds,
            "cpu_per_round": cpu / max(1, rounds) * 1000}


SCENARIOS = {"config": config, "tools": tools, "session": session, "budget": budget, "turn": turn}


if __name__ == "__main__":
    name = sys.argv[1]
    real = _quiet()
    from js import paths

    paths.configure_home()
    result = SCENARIOS[name]()
    real.write(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in result.items()}) + "\n")
