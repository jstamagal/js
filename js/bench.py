"""`js --bench AGENT`: run an agent over its benchmark cases and print one row per case."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from . import (
    agent_messages,
    debug_trace,
    headless,
    messages as msgs,
    output,
    owning_loop,
    persona as P,
    run_assembly,
    runtime,
    setting_values,
    snippets,
    stats,
)
from .config import (
    InvocationSources,
    validate_agent_id,
)


class _BenchTools:
    """Reserve model call order before parallel tools report their outcomes."""

    outcomes = frozenset({"tool_ok", "tool_error", "tool_exception", "tool_invalid", "tool_call_refused", "tool_unknown"})

    def __init__(self) -> None:
        self.entries: list[list] = []
        self.calls: dict[str, list] = {}

    def event(self, kind: str, fields: dict) -> None:
        call_id = fields.get("call_id")
        if kind == "tool_scheduled":
            entry = [str(fields["tool"]), False]
            self.entries.append(entry)
            self.calls[call_id] = entry
        elif kind in self.outcomes:
            name = ("?" if kind == "tool_unknown" else "") + str(fields["tool"])
            entry = self.calls.get(call_id) if call_id else None
            if entry is None:
                self.entries.append([name, True])
            else:
                entry[:] = [name if not entry[0].startswith("?") else entry[0], True]

    @property
    def names(self) -> list[str]:
        return [name for name, finished in self.entries if finished]


def _bench_row_line(row: dict) -> msgs.Said:
    if not row.get("ok"):
        return msgs.BENCH_FAILED.said(name=row["name"], error=row.get("error") or "failed")
    ttft = f"{row['ttft_s'] * 1000:.0f}ms" if row.get("ttft_s") is not None else "—"
    return msgs.BENCH_ROW.said(name=row["name"], tokens=row.get("output_tokens", 0),
                               tps=row.get("tok_per_s", 0.0), ttft=ttft, wall=row.get("wall_s") or 0.0)


def run_bench_mode(args: argparse.Namespace, sources: InvocationSources) -> int:
    """`js --bench AGENT`. It runs AGENT, so --agent, --commit or --compact
    beside it exits 2."""
    if args.commit or args.compact or args.agent:
        msgs.warn(msgs.BENCH_EXCLUSIVE)
        return 2
    return run_agent_bench(args.bench, args, sources)


def run_agent_bench(agent: str, args: argparse.Namespace, sources: InvocationSources) -> int:
    """Benchmark `agent` with the model, reasoning, output cap, quiet, stats and
    debug flags of the command line."""
    return run_bench(agent, model=args.model, reasoning=args.reasoning,
                     maxout=args.max_out, quiet=args.quiet, sources=sources,
                     stats_json=args.stats_json, stats_csv=args.stats_csv,
                     debug=args.debug)


def run_bench(bench_agent: str, *, model: str | None, reasoning: str | None,
               maxout: int | None, quiet: bool, sources: InvocationSources,
               stats_json: str | None, stats_csv: str | None,
               debug: bool = False) -> int:
    """Run an agent's NN-benchmark.md turns, each on a clean slate (fresh
    context, no session), measuring TTFT / tok-s / turn time. The persona
    (NN-prompt.md + agent.yaml) is rebuilt into each benchmark's head;
    benchmarks never see each other."""
    try:
        agent_id = validate_agent_id(bench_agent)
    except (ValueError, OSError) as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2
    reasoning_override = None
    if reasoning is not None:
        reasoning_override, effort_error = setting_values.validate_cli_reasoning(reasoning)
        if effort_error is not None:
            msgs.warn(msgs.BAD_REASONING, value=reasoning, error=effort_error)
            return 2
    try:
        cfg = run_assembly.from_env(None, save_session=False, sources=sources, agent_id=agent_id)
        cfg = replace(cfg, session_file=Path(os.devnull))
    except ValueError as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2
    try:
        settled = run_assembly.settle(cfg, model=model, reasoning=reasoning_override, config_pins=True)
    except run_assembly.RunNotSettled as e:
        msgs.warn(msgs.FAILED, error=P.format_prompt_load_error(cfg, e.cause) if e.loading else e.cause)
        return 2
    except (FileNotFoundError, ValueError) as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2
    try:
        benchmarks = P.load_benchmarks(cfg.prompts_dir)
    except (FileNotFoundError, ValueError) as e:
        msgs.warn(msgs.FAILED, error=P.format_prompt_load_error(cfg, e))
        return 2

    if not benchmarks:
        msgs.warn(msgs.NO_BENCHMARKS, agent=agent_id)
        return 2

    cfg = replace(settled.config, settings={**settled.config.settings, "REASONING_DISPLAY": 3,
                                            "TOOLS_DISPLAY": 3, "TOOLS_QUIET": [], "SHELL_QUIET": [],
                                            "MARKDOWN": False})
    prompt_spec = settled.prompt_spec
    events = headless.active
    if events is not None:
        events.emit("start", version=headless.SCHEMA_VERSION, agent=agent_id,
                    model=cfg.model, provider=cfg.provider_id)
    # The resolved OUTPUT_TOKENS_MAX: agent.yaml's max_tokens is one of its layers.
    agent_default_max = cfg.max_output_tokens
    system = settled.system

    # The request trace (system prompt, tools, messages, FLIGHT lines) goes to
    # logs/<agent>/<agent>-<pid>.log like every other run. A bench keeps no session,
    # so without this nothing records what the model was sent.
    autolog = debug_trace.debug_autolog_path(cfg, getattr(cfg, "settings", {}) or {})
    trace_sink = debug_trace.open_debug_trace_sink([autolog] if autolog is not None else [])

    rows: list[dict] = []
    interrupted = False
    for bench in benchmarks:
        # max_tokens: --max-out wins; else per-benchmark frontmatter (already
        # -1 -> None=uncapped); else the agent default from agent.yaml.
        if maxout is not None:
            eff_max = maxout
        elif bench.max_tokens_set:
            eff_max = bench.max_tokens
        else:
            eff_max = agent_default_max
        prompt_text = bench.prompt
        messages = []
        call_stats: list[dict] = []
        tool_names = _BenchTools()
        turn_kwargs = settled.turn_kwargs(max_output_override=eff_max, call_stats=call_stats)
        if events is not None:
            turn_kwargs["event_sink"] = events.runtime_event
        ok, err = True, None
        t_wall = time.time()
        try:
            sink = io.StringIO() if quiet else None
            out = sink if sink is not None else (debug_trace.StdoutTee(sys.stdout, trace_sink) if trace_sink is not None else None)
            with contextlib.redirect_stdout(out) if out is not None else contextlib.nullcontext():
                async def run_benchmark():
                    nonlocal prompt_text
                    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
                    telemetry.trace_sink = trace_sink
                    telemetry.event_observer = tool_names.event
                    if events is not None:
                        telemetry.sink = output.Fanout(output.sink_of(telemetry), headless.OutputEvents(events))
                    await agent_messages.initialize(
                        cfg, prompt_spec, messages, telemetry, save=False,
                        trace_override=True, **turn_kwargs,
                    )
                    prompt_text = P.expand_agent_text(bench.prompt, cfg)
                    if not quiet:
                        first = (prompt_text.splitlines() or [""])[0][:80]
                        msgs.warn(msgs.BENCH_START, name=bench.name, prompt=first)
                    messages.append({"role": "user", "content": prompt_text})
                    await runtime.run_turn_async(
                        cfg, system, messages, telemetry, trace_override=True, **turn_kwargs,
                    )

                owning_loop.run_owning_loop(run_benchmark())
        except KeyboardInterrupt:
            interrupted, ok, err = True, False, "interrupted"
        except snippets.SnippetError:
            raise
        except Exception as e:  # noqa: BLE001
            ok, err = False, f"{type(e).__name__}: {e}"
        row = {
            "name": bench.name, "prompt": prompt_text, "max_tokens": eff_max,
            "ok": ok, "error": err, "tools": tool_names.names,
            **stats.summarize_calls(call_stats, wall_s=time.time() - t_wall),
        }
        rows.append(row)
        msgs.say_said(_bench_row_line(row), file=sys.stderr)
        if interrupted:
            break

    if trace_sink is not None:
        trace_sink.close()

    payload = {"agent": agent_id, "model": cfg.model, "provider": cfg.provider_id, "benchmarks": rows}
    stats_ok = stats.write_requested(stats_json, stats_csv, payload, rows, announce=True)
    if events is not None:
        events.result = payload
    elif not stats_json and not stats_csv:
        print(json.dumps(payload, indent=2, default=str))
    return 130 if interrupted else 1 if not stats_ok or any(not row["ok"] for row in rows) else 0

