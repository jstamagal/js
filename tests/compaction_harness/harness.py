"""Independent adversarial compaction probes; no js test helpers or fixtures.
Usage: uv run python harness.py REPOSITORY OUTPUT_DIR
"""

import asyncio
import copy
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

REPO, OUT = map(Path, sys.argv[1:3])
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO))
os.environ["JS_VISION"] = "false"
os.environ["XDG_STATE_HOME"] = str(OUT / "state")
os.environ["XDG_CONFIG_HOME"] = str(OUT / "config")
os.environ["XDG_CACHE_HOME"] = str(OUT / "cache")
from js import (  # noqa: E402
    cli,
    compaction as C,
    context_budget as B,
    memory as M,
    model_client as MC,
    runtime as R,
)  # noqa: E402
from js.config import Config  # noqa: E402
from js.toolkit.core import Tool, ToolContext  # noqa: E402
from js.toolkit.registry import ToolRegistry  # noqa: E402

ai = MC.ai
RESULTS = []


def cfg_for(name, **compact):
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    return Config(
        agent_id="adversary",
        agent_dir=d,
        model="adversary",
        provider_id="openai",
        provider_base_url="http://127.0.0.1:1/v1",
        provider_api_key="fixture",
        reasoning_effort=None,
        max_output_tokens=100,
        max_tool_iterations=8,
        max_bash_output_bytes=100000,
        max_tool_result_bytes=100000,
        fetch_timeout_s=1,
        debug_log=None,
        trace=False,
        history_file=d / "history.jsonl",
        sessions_dir=d,
        session_file=d / "session.jsonl",
        prompts_dir=d,
        project_dir=d,
        settings={
            "compact": {
                "auto": True,
                "context_window": 10000,
                "buffer_tokens": 100,
                "summary_reserve_tokens": 100,
                "tail_tokens": 100,
                "min_savings_tokens": 20,
                "flight_log_dir": str(d / "flights"),
                **compact,
            }
        },
    )


def result(text, calls=(), input_tokens=100, cache=0, output_tokens=10, incomplete=None):
    parts = [ai.types.messages.TextPart(text=text)] if text else []
    parts += [
        ai.types.messages.ToolCallPart(tool_call_id=c.id, tool_name=c.name, tool_args=c.arguments)
        for c in calls
    ]
    return MC.ModelStreamResult(
        text=text,
        tool_calls=list(calls),
        reasoning="",
        usage=ai.types.usage.Usage(
            input_tokens=input_tokens, cache_read_tokens=cache, output_tokens=output_tokens
        ),
        finish_reason="incomplete:" + incomplete
        if incomplete
        else "tool_calls"
        if calls
        else "stop",
        assistant_message=ai.assistant_message(*parts),
        incomplete_reason=incomplete,
    )


def overflow():
    return ai.ProviderAPIError(
        "context_length_exceeded",
        provider="openai",
        code="context_length_exceeded",
        is_retryable=False,
    )


def text_of(messages):
    return "\n".join(
        str(getattr(p, "text", getattr(p, "result", ""))) for m in messages for p in m.parts
    )


def facts(text):
    return sorted(set(re.findall(r"FACT_[0-9]+", text)))


def observed(name, checks, **evidence):
    rec = {"name": name, "pass": all(checks.values()), "checks": checks, "evidence": evidence}
    RESULTS.append(rec)
    print(json.dumps(rec, default=str), flush=True)


def persist_turn(cfg, messages, user):
    import inspect

    if "user_recorded" in inspect.signature(cli._persist_turn_messages).parameters:
        cli._persist_turn_messages(cfg, messages, user, user_recorded=True)
    else:
        cli._persist_turn_messages(cfg, messages)


async def summary_fault(mode):
    cfg = cfg_for("summary_" + mode)
    context = ToolContext(cwd=cfg.agent_dir)
    messages = [
        {"role": "user" if n % 2 == 0 else "assistant", "content": f"FACT_{n} " + "x " * 400}
        for n in range(8)
    ]
    messages.append({"role": "user", "content": "current question " + "z " * 300})
    before = copy.deepcopy(messages)
    for m in messages:
        M.append_message(cfg.session_file, m)
    attempts = []

    async def provider(**kw):
        payload = text_of(kw["messages"])
        seen = facts(payload)
        attempts.append(seen)
        if mode == "empty":
            return result("")
        if mode == "truncated":
            return result("partial", incomplete="max_output_tokens")
        if mode == "overflow" and len(seen) > 2:
            raise overflow()
        return result(" ".join(seen))

    error = None
    with (
        patch.object(MC, "stream_model_async", provider),
        patch.object(R.T, "DEFAULT_CONTEXT", context),
    ):
        try:
            await C.compact_now(cfg, "", messages, forced=True)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    after_facts = facts(json.dumps(messages))
    expected = facts(json.dumps(before))
    protected = messages == before if mode in ("empty", "truncated") else after_facts == expected
    if error and not error.startswith(("ValueError:", "RuntimeError:", "ProviderAPIError:")):
        raise AssertionError(error)
    observed(
        "summary_" + mode,
        {"source_protected": protected},
        error=error,
        attempts=attempts,
        before_facts=expected,
        after_facts=after_facts,
        history_changed=messages != before,
    )


async def no_prefix():
    cfg = cfg_for("no_prefix", tail_tokens=99999)
    messages = [{"role": "user", "content": "one short input"}]
    before = copy.deepcopy(messages)
    calls = 0

    async def provider(**kw):
        nonlocal calls
        calls += 1
        return result("invented summary")

    with (
        patch.object(MC, "stream_model_async", provider),
        patch.object(R.T, "DEFAULT_CONTEXT", ToolContext(cwd=cfg.agent_dir)),
    ):
        outcome = await C.compact_now(cfg, "", messages, forced=True, preserve_from=0)
    observed(
        "no_prefix",
        {"no_empty_summary": calls == 0, "history_unchanged": messages == before},
        calls=calls,
        outcome=outcome,
    )


async def expanding_summary():
    cfg = cfg_for("expanding_summary", tail_tokens=1)
    messages = [
        {"role": "user", "content": "short old message"},
        {"role": "user", "content": "last question"},
    ]
    before = copy.deepcopy(messages)

    async def provider(**kw):
        return result("expanded " * 1000)

    with (
        patch.object(MC, "stream_model_async", provider),
        patch.object(R.T, "DEFAULT_CONTEXT", ToolContext(cwd=cfg.agent_dir)),
    ):
        outcome = await C.compact_now(cfg, "", messages, forced=True)
    observed(
        "expanding_summary",
        {"no_growth_committed": len(json.dumps(messages)) <= len(json.dumps(before))},
        outcome=outcome,
        before_chars=len(json.dumps(before)),
        after_chars=len(json.dumps(messages)),
    )


async def rehydration():
    cfg = cfg_for("rehydration", tail_tokens=1)
    file = cfg.agent_dir / "edited.py"
    file.write_text("FACT_99 = 1\n")
    context = ToolContext(cwd=cfg.agent_dir)
    context.read_paths.add(file)
    messages = [
        {"role": "user", "content": "old " * 1000},
        {"role": "assistant", "content": "answer " * 1000},
        {"role": "user", "content": "next question"},
    ]
    for m in messages:
        M.append_message(cfg.session_file, m)

    async def provider(**kw):
        return result("summary")

    with (
        patch.object(MC, "stream_model_async", provider),
        patch.object(R.T, "DEFAULT_CONTEXT", context),
    ):
        await C.compact_now(cfg, "", messages, forced=True)
    loaded = M.load_messages(cfg.session_file)
    observed(
        "rehydration",
        {"restart_matches_live": loaded == messages},
        live_roles=[m["role"] for m in messages],
        loaded_roles=[m["role"] for m in loaded],
        live_facts=facts(json.dumps(messages)),
        loaded_facts=facts(json.dumps(loaded)),
    )


async def between_turn():
    cfg = cfg_for("between_turn", tail_tokens=1)
    context = ToolContext(cwd=cfg.agent_dir)
    # Provider input is 6000; 3200 new output makes next request 9200 of 9800.
    messages = [
        {"role": "user", "content": "old " * 10000},
        {"role": "assistant", "content": "new " * 3200},
    ]
    tracker = B.TokenState()
    tracker.record_provider_usage(
        SimpleNamespace(input_tokens=6000, output_tokens=3200), message_count=2, messages=messages
    )
    context.context_budget_state = tracker
    context.last_prompt_tokens = 6000
    context.last_output_tokens = 3200
    called = []

    def compact(*a, **kw):
        called.append(kw)
        return "compact skipped: no useful savings"

    ac = C.AutoCompactState()

    async def call_policy():
        if hasattr(C, "maybe_auto_compact_async"):
            with patch.object(C, "compact_now", AsyncMock(side_effect=compact)):
                return await C.maybe_auto_compact_async(
                    cfg, ac, context, "", messages, lambda: 10000
                )
        with patch.object(C, "compact_now_sync", compact):
            return C.maybe_auto_compact(cfg, ac, context, "", messages, lambda: 10000)

    out = await call_policy()
    observed(
        "between_turn_output",
        {"new_output_included": bool(called)},
        calls=len(called),
        outcome=asdict(out),
    )
    context.last_prompt_tokens = 9500
    out = await call_policy()
    observed(
        "between_turn_skip",
        {"skip_not_success": not out.compacted, "skip_not_consecutive": ac.consecutive == 0},
        outcome=asdict(out),
        auto=asdict(ac),
    )


async def run_loop(mode):
    cfg = cfg_for(mode, context_window=1500 if mode == "persist_active" else 10000, tail_tokens=100)
    context = ToolContext(cwd=cfg.agent_dir)
    if mode == "churn":
        prefix = C._compaction_summary_message("previous summary " * 100)
        messages = [prefix, {"role": "user", "content": "current task"}]
    elif mode == "persist_active":
        messages = [{"role": "user", "content": "current task"}]
    else:
        messages = [
            {"role": "user", "content": "older " * 1200},
            {"role": "assistant", "content": "old reply " * 1000},
            {"role": "user", "content": "current task"},
        ]
    user = messages[-1]
    for m in messages:
        M.append_message(cfg.session_file, m)
    summaries = []
    requests = []
    effects = []
    model_calls = 0
    error = None

    def tool(context):
        effects.append(len(effects))
        return "fresh tool data " * 1000 if mode == "persist_active" else "small result"

    registry = ToolRegistry(
        tools=(Tool("audit_action", "fixture action", tool, params={}),), aliases={}
    )

    async def provider(**kw):
        nonlocal model_calls
        payload = text_of(kw["messages"])
        if payload.startswith("Summarize this js session"):
            summaries.append(json.loads(payload.split("Session messages JSON:\n", 1)[1]))
            return result("condensed " + str(len(summaries)))
        requests.append([m.model_dump(mode="json") for m in kw["messages"]])
        model_calls += 1
        if mode == "midturn_overflow" and model_calls == 2:
            raise overflow()
        if model_calls < 5:
            call = MC.ModelToolCall(id=f"call_{model_calls}", name="audit_action", arguments="{}")
            return result(
                "", [call], input_tokens=10200 if mode == "churn" else 400, output_tokens=20
            )
        return result("finished", input_tokens=400)

    with (
        patch.object(MC, "stream_model_async", provider),
        patch.object(R.T, "DEFAULT_CONTEXT", context),
    ):
        try:
            await R.run_turn_async(
                cfg,
                "",
                messages,
                R.Telemetry(cfg.agent_dir / "events.jsonl"),
                tool_context=context,
                tool_registry=registry,
                suppress_output=True,
            )
            persist_turn(cfg, messages, user)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    loaded = M.load_messages(cfg.session_file)
    checks = {
        "turn_finishes": messages[-1].get("content") == "finished" and error is None,
        "disk_replay_matches": loaded == messages,
    }
    if mode == "churn":
        checks["no_summary_only_rewrites"] = all(
            any(
                not (
                    m.get("role") == "user"
                    and m.get("content", "").startswith(
                        ("<compaction-summary>", "<post-compaction-files>")
                    )
                )
                for m in source
            )
            for source in summaries
        )
    if mode == "midturn_overflow":
        checks["tools_once"] = len(effects) == 3
    observed(
        mode,
        checks,
        error=error,
        model_calls=model_calls,
        summaries=len(summaries),
        tool_executions=len(effects),
        user_survives=any(m is user for m in messages),
        live_count=len(messages),
        replay_count=len(loaded),
    )
    (cfg.agent_dir / "requests.json").write_text(json.dumps(requests, indent=2))
    (cfg.agent_dir / "live.json").write_text(json.dumps(messages, indent=2))
    (cfg.agent_dir / "replay.json").write_text(json.dumps(loaded, indent=2))


async def cancelled_summary():
    cfg = cfg_for("cancelled_summary", tail_tokens=1)
    messages = [
        {"role": "user", "content": "prefix " * 1000},
        {"role": "user", "content": "current"},
    ]
    before = copy.deepcopy(messages)
    for m in messages:
        M.append_message(cfg.session_file, m)
    original_bytes = cfg.session_file.read_bytes()

    async def provider(**kw):
        raise asyncio.CancelledError()

    cancelled = False
    with patch.object(MC, "stream_model_async", provider):
        try:
            await C.compact_now(cfg, "", messages, forced=True)
        except asyncio.CancelledError:
            cancelled = True
    observed(
        "cancelled_summary",
        {
            "cancellation_propagated": cancelled,
            "memory_unchanged": messages == before,
            "disk_unchanged": cfg.session_file.read_bytes() == original_bytes,
        },
    )


async def partial_partition_failure():
    cfg = cfg_for("partial_partition_failure", tail_tokens=1)
    messages = [{"role": "user", "content": f"FACT_{n} " + "text " * 400} for n in range(8)]
    messages.append({"role": "user", "content": "current"})
    before = copy.deepcopy(messages)
    calls = []

    async def provider(**kw):
        seen = facts(text_of(kw["messages"]))
        calls.append(seen)
        if len(seen) > 2:
            raise overflow()
        if "FACT_6" in seen:
            raise ai.ProviderAPIError("server unavailable", provider="openai", is_retryable=True)
        return result(" ".join(seen))

    with patch.object(MC, "stream_model_async", provider):
        try:
            await C.compact_now(cfg, "", messages, forced=True)
        except ai.ProviderAPIError:
            pass
    observed("partial_partition_failure", {"source_unchanged": messages == before}, calls=calls)


async def context_isolation():
    cfg = cfg_for("context_isolation", tail_tokens=1)
    own = ToolContext(cwd=cfg.agent_dir)
    foreign = ToolContext(cwd=cfg.agent_dir)
    a = cfg.agent_dir / "own.py"
    a.write_text("FACT_11=1")
    b = cfg.agent_dir / "foreign.py"
    b.write_text("FACT_22=2")
    own.read_paths.add(a)
    foreign.read_paths.add(b)
    messages = [
        {"role": "user", "content": "prefix " * 2000},
        {"role": "user", "content": "current"},
    ]
    own.context_budget_state = B.TokenState()
    foreign.context_budget_state = B.TokenState()
    own.context_budget_state.record_provider_usage(
        SimpleNamespace(input_tokens=2000, output_tokens=10), message_count=2, messages=messages
    )
    foreign.context_budget_state.record_provider_usage(
        SimpleNamespace(input_tokens=4000, output_tokens=10), message_count=2, messages=messages
    )

    async def provider(**kw):
        return result("summary")

    import inspect

    supports = "context" in inspect.signature(C.compact_now).parameters
    with (
        patch.object(MC, "stream_model_async", provider),
        patch.object(R.T, "DEFAULT_CONTEXT", foreign),
    ):
        await C.compact_now(
            cfg, "", messages, forced=True, **({"context": own} if supports else {})
        )
    seen = facts(json.dumps(messages))
    observed(
        "context_isolation",
        {
            "own_files_only": seen == ["FACT_11"],
            "own_anchor_reset": own.context_budget_state.last_usage is None,
            "foreign_anchor_untouched": foreign.context_budget_state.last_usage is not None,
        },
        facts=seen,
    )


async def accounting_properties():
    messages = [{"role": "user", "content": "x" * 400}, {"role": "assistant", "content": "y" * 400}]
    tracker = B.TokenState()
    usage = SimpleNamespace(input_tokens=100, output_tokens=100, cache_read_tokens=90)
    tracker.record_provider_usage(usage, message_count=2, messages=messages)
    calibration = tracker.calibrated_chars_per_token(messages=messages)
    estimate = B.estimate_request_tokens(messages=messages).total_tokens
    expected = 4 * estimate / 200
    # A compaction changed the prefix but kept its length.
    changed = [{"role": "user", "content": "short summary"}, messages[-1]]
    observed(
        "accounting_properties",
        {
            "cache_subset_once": tracker.current_context_tokens(messages=messages)[0] == 200,
            "response_count_calibrates_response_prefix": abs(calibration - expected) < 1e-8,
            "stale_prefix_not_calibrated": tracker.calibrated_chars_per_token(messages=changed)
            == 4,
        },
        calibration=calibration,
        expected=expected,
    )


async def clearing_restart():
    cfg = cfg_for("clearing_restart")
    messages = [{"role": "user", "content": "old task"}]
    for n in range(24):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": str(n),
                            "type": "function",
                            "function": {"name": "audit_action", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": str(n),
                    "name": "audit_action",
                    "content": f"FACT_{n} " + "x" * 2000,
                },
            ]
        )
    for m in messages:
        M.append_message(cfg.session_file, m)
    action, cleared, _ = C.recover_overflow(
        messages, 1, cfg=cfg, system="", error=overflow(), flight_data={}
    )
    observed(
        "clearing_restart",
        {
            "cleared_old_results": cleared == 4,
            "replay_matches": M.load_messages(cfg.session_file) == messages,
        },
        action=action,
        cleared=cleared,
    )


async def zero_usage_executor():
    cfg = cfg_for("zero_usage_executor", tail_tokens=1)
    context = ToolContext(cwd=cfg.agent_dir)
    user = {"role": "user", "content": "current question"}
    messages = [
        {"role": "user", "content": "old " * 12000},
        {"role": "assistant", "content": "answer"},
        user,
    ]
    for m in messages:
        M.append_message(cfg.session_file, m)
    from js.persona import PromptSpec
    from js.sampling import Sampling

    state = {
        "settings": cfg.settings,
        "system": "",
        "messages": messages,
        "tool_registry": ToolRegistry(tools=(), aliases={}),
        "sampling_cli": Sampling(),
        "model": cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
    }
    calls = 0
    error = None

    async def turn(*args, **kw):
        # A provider returned text without usage (or a zero-usage turn ended).
        messages.append({"role": "assistant", "content": "finished"})

    async def provider(**kw):
        nonlocal calls
        calls += 1
        return result("summary")

    with (
        patch.object(R, "run_turn_async", turn),
        patch.object(MC, "stream_model_async", provider),
        patch.object(R.T, "DEFAULT_CONTEXT", context),
    ):
        try:
            await cli._do_turn(
                cfg,
                state,
                R.Telemetry(None),
                PromptSpec("", ()),
                SimpleNamespace(runtime_message=user, history_message=user),
                cfg,
                2,
                asyncio.get_running_loop(),
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    observed(
        "zero_usage_executor",
        {"async_cli_completes": error is None, "summary_runs": calls > 0},
        error=error,
        calls=calls,
    )


async def recovery_rounds():
    cfg = cfg_for("recovery_rounds", auto=False, tail_tokens=1)
    context = ToolContext(cwd=cfg.agent_dir)
    messages = [{"role": "user", "content": "old task"}]
    for n in range(45):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": str(n),
                            "type": "function",
                            "function": {"name": "audit_action", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": str(n),
                    "name": "audit_action",
                    "content": "x" * 2000,
                },
            ]
        )
    messages.append({"role": "user", "content": "current"})
    calls = 0
    error = None

    async def provider(**kw):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise overflow()
        return result("finished")

    with (
        patch.object(MC, "stream_model_async", provider),
        patch.object(R.T, "DEFAULT_CONTEXT", context),
    ):
        try:
            await R.run_turn_async(
                cfg,
                "",
                messages,
                R.Telemetry(None),
                tool_registry=ToolRegistry(tools=(), aliases={}),
                tool_context=context,
                suppress_output=True,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    observed(
        "recovery_rounds",
        {"each_recovery_retried": calls == 4, "finishes": messages[-1]["content"] == "finished"},
        error=error,
        calls=calls,
    )


async def small_window():
    cfg = cfg_for("small_window", context_window=4000, buffer_tokens=4096)
    context = ToolContext(cwd=cfg.agent_dir)
    messages = [{"role": "user", "content": "short question"}]
    budgets = []

    class Telemetry(R.Telemetry):
        def event(self, kind, **fields):
            if kind == "context_budget":
                budgets.append(fields)

    async def provider(**kw):
        return result("finished")

    with (
        patch.object(MC, "stream_model_async", provider),
        patch.object(R.T, "DEFAULT_CONTEXT", context),
    ):
        await R.run_turn_async(
            cfg,
            "",
            messages,
            Telemetry(None),
            tool_registry=ToolRegistry(tools=(), aliases={}),
            tool_context=context,
            suppress_output=True,
        )
    observed(
        "small_window",
        {
            "same_effective_budget": all(
                b["effective_input_limit"] == C.effective_context_window(cfg, 4000) for b in budgets
            )
        },
        budgets=budgets,
    )


async def journal_sequence():
    import random

    rng = random.Random(441)
    cfg = cfg_for("journal_sequence", tail_tokens=35)
    messages = []
    mismatches = []
    operations = []

    async def provider(**kw):
        return result("summary")

    context = ToolContext(cwd=cfg.agent_dir)
    for step in range(60):
        op = rng.choice(("append", "append", "clear", "compact", "resume"))
        if op == "append":
            messages.append(
                {"role": "user", "content": f"question {step} " + "data " * rng.randint(1, 250)}
            )
            messages.append({"role": "assistant", "content": f"reply {step}"})
        elif op == "clear" and messages:
            index = rng.randrange(len(messages))
            messages[index] = {**messages[index], "content": "short"}
        elif op == "compact":
            with (
                patch.object(MC, "stream_model_async", provider),
                patch.object(R.T, "DEFAULT_CONTEXT", context),
            ):
                await C.compact_now(cfg, "", messages, forced=True)
        elif op == "resume":
            messages = M.load_messages(cfg.session_file)
        if hasattr(M, "persist_messages"):
            M.persist_messages(cfg.session_file, messages)
        else:
            # Legacy CLI persistence only appends current-turn suffix. Compare the current implementation directly.
            if messages:
                cli._persist_turn_messages(cfg, messages, messages[-1], user_recorded=False)
        if M.load_messages(cfg.session_file) != messages:
            mismatches.append(step)
        operations.append(op)
    observed(
        "journal_sequence",
        {"all_replays_match": not mismatches},
        operations=len(operations),
        mismatches=mismatches,
    )


async def tail_budget():
    messages = [
        {"role": "user", "content": "old " * 15000},
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "continue"},
    ]
    start = C._safe_tail_start(messages, 100, 4)
    observed("tail_budget", {"oversized_old_message_not_kept": start == 1}, keep_from=start)


async def incomplete_usage():
    tracker = B.TokenState()
    messages = [
        {"role": "user", "content": "x " * 10000},
        {"role": "assistant", "content": "answer"},
    ]
    tracker.record_provider_usage(
        SimpleNamespace(input_tokens=None, output_tokens=20), message_count=2, messages=messages
    )
    count, _, used = tracker.current_context_tokens(messages=messages)
    observed(
        "incomplete_usage",
        {"output_only_usage_not_prompt_anchor": count >= 5000 and not used},
        count=count,
        used_provider=used,
    )


async def compacted_turn_cancel(error=False):
    cfg = cfg_for("compacted_turn_error" if error else "compacted_turn_cancel", auto=False)
    context = ToolContext(cwd=cfg.agent_dir)
    prefix = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"old {i} " * 200}
        for i in range(20)
    ]
    user = {"role": "user", "content": "current request"}
    messages = [*prefix, user]
    for m in messages:
        M.append_message(cfg.session_file, m)
    from js.persona import PromptSpec
    from js.sampling import Sampling

    state = {
        "settings": cfg.settings,
        "system": "",
        "messages": messages,
        "tool_registry": ToolRegistry(tools=(), aliases={}),
        "sampling_cli": Sampling(),
        "model": cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
    }

    async def turn(*args, **kw):
        context.compacted_during_turn = True
        M.append_compaction_mark(cfg.session_file, summary="previous work", keep_from=20)
        messages[:] = [
            C._compaction_summary_message("previous work"),
            user,
            {"role": "assistant", "content": "partial new work"},
        ]
        if error:
            raise ai.ProviderAPIError("unavailable", provider="openai")
        raise asyncio.CancelledError()

    with patch.object(R, "run_turn_async", turn), patch.object(R.T, "DEFAULT_CONTEXT", context):
        try:
            await cli._do_turn(
                cfg,
                state,
                R.Telemetry(None),
                PromptSpec("", ()),
                SimpleNamespace(runtime_message=user, history_message=user),
                cfg,
                20,
                asyncio.get_running_loop(),
            )
        except asyncio.CancelledError:
            pass
    loaded = M.load_messages(cfg.session_file)
    observed(
        "compacted_turn_error" if error else "compacted_turn_cancel",
        {
            "partial_kept": any(m.get("content") == "partial new work" for m in loaded),
            "disk_matches_live": loaded == messages,
        },
        live=messages,
        loaded=loaded,
    )


async def between_turn_cancel():
    import threading

    cfg = cfg_for("between_turn_cancel", tail_tokens=1)
    context = ToolContext(cwd=cfg.agent_dir)
    context.last_prompt_tokens = 9500
    user = {"role": "user", "content": "current"}
    messages = [{"role": "user", "content": "old " * 12000}, user]
    for m in messages:
        M.append_message(cfg.session_file, m)
    from js.persona import PromptSpec
    from js.sampling import Sampling

    state = {
        "settings": cfg.settings,
        "system": "",
        "messages": messages,
        "tool_registry": ToolRegistry(tools=(), aliases={}),
        "sampling_cli": Sampling(),
        "model": cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
    }
    started = threading.Event()
    released = threading.Event()
    ended = threading.Event()

    async def turn(*args, **kw):
        messages.append({"role": "assistant", "content": "finished"})

    async def provider(**kw):
        started.set()
        try:
            while not released.is_set():
                await asyncio.sleep(0.005)
            return result("summary")
        finally:
            ended.set()

    with (
        patch.object(R, "run_turn_async", turn),
        patch.object(MC, "stream_model_async", provider),
        patch.object(R.T, "DEFAULT_CONTEXT", context),
    ):
        task = asyncio.create_task(
            cli._do_turn(
                cfg,
                state,
                R.Telemetry(None),
                PromptSpec("", ()),
                SimpleNamespace(runtime_message=user, history_message=user),
                cfg,
                1,
                asyncio.get_running_loop(),
            )
        )
        try:
            if not await asyncio.to_thread(started.wait, 3):
                raise AssertionError("summary never started")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            after_cancel = copy.deepcopy(messages)
            released.set()
            if not await asyncio.to_thread(ended.wait, 3):
                raise AssertionError("provider did not close")
            await asyncio.sleep(0.1)
        finally:
            released.set()
            if not task.done():
                task.cancel()
    observed(
        "between_turn_cancel",
        {
            "cancel_stops_summary_commit": messages == after_cancel,
            "disk_matches_live": M.load_messages(cfg.session_file) == messages,
        },
        before_count=len(after_cancel),
        after_count=len(messages),
    )


async def main():
    probes = [
        *(lambda mode=mode: summary_fault(mode) for mode in ("empty", "truncated", "overflow")),
        no_prefix,
        expanding_summary,
        rehydration,
        between_turn,
        cancelled_summary,
        partial_partition_failure,
        context_isolation,
        accounting_properties,
        clearing_restart,
        zero_usage_executor,
        recovery_rounds,
        small_window,
        journal_sequence,
        tail_budget,
        incomplete_usage,
        between_turn_cancel,
        compacted_turn_cancel,
        lambda: compacted_turn_cancel(True),
        *(
            lambda mode=mode: run_loop(mode)
            for mode in ("churn", "midturn_overflow", "persist_active")
        ),
    ]
    for probe in probes:
        try:
            await probe()
        except Exception as exc:
            import traceback

            traceback.print_exc()
            observed(
                getattr(probe, "__name__", "probe"),
                {"harness_completed": False},
                error=f"{type(exc).__name__}: {exc}",
            )
    (OUT / "results.json").write_text(json.dumps(RESULTS, indent=2))
    print(
        "TOTAL",
        len(RESULTS),
        "PASS",
        sum(r["pass"] for r in RESULTS),
        "FAIL",
        sum(not r["pass"] for r in RESULTS),
    )
    return all(r["pass"] for r in RESULTS)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
