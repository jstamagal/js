"""--bench mode: persona benchmark loading, max_tokens resolution, stats emit."""

from __future__ import annotations

import json
from pathlib import Path

import ai
import ai.types.usage

from js import cli, persona, run_assembly
from js.model_client import ModelStreamResult


def _write_agent(root: Path) -> Path:
    """A joker-shaped agent dir: persona prompts + a tools manifest with a
    max_tokens default + four benchmarks exercising every max_tokens case."""
    d = root / ".js" / "agents" / "jokertest"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text("max_tokens: 4096\ntools:\n  - read:lazy\n", encoding="utf-8")
    (d / "01-prompt.md").write_text("# Role\nYou answer tersely.", encoding="utf-8")
    (d / "02-prompt.md").write_text("PWD is {{PWD}}", encoding="utf-8")
    (d / "02-benchmark.md").write_text("---\nmax_tokens: 512\n---\n\nName 5 facts about Germany.", encoding="utf-8")
    (d / "03-benchmark.md").write_text("---\nmax_tokens: -1\n---\n\nUncapped one.", encoding="utf-8")
    (d / "04-benchmark.md").write_text("No frontmatter — inherits the agent default.", encoding="utf-8")
    (d / "01-benchmark.md").write_text("---\nmax_tokens: 256\n---\n\nFirst by sort order.", encoding="utf-8")
    return d


def test_load_benchmarks_orders_and_parses(tmp_path):
    d = _write_agent(tmp_path)
    benches = persona.load_benchmarks(d)

    # Sorted by filename, so 01 before 02/03/04.
    assert [b.name for b in benches] == ["01-benchmark", "02-benchmark", "03-benchmark", "04-benchmark"]
    by_name = {b.name: b for b in benches}
    assert by_name["01-benchmark"].max_tokens == 256 and by_name["01-benchmark"].max_tokens_set
    assert by_name["02-benchmark"].max_tokens == 512 and by_name["02-benchmark"].max_tokens_set
    # -1 coerces to None (uncapped) but the key WAS set.
    assert by_name["03-benchmark"].max_tokens is None and by_name["03-benchmark"].max_tokens_set
    # No frontmatter at all -> not set, falls back to agent default downstream.
    assert by_name["04-benchmark"].max_tokens is None and not by_name["04-benchmark"].max_tokens_set
    assert by_name["02-benchmark"].prompt == "Name 5 facts about Germany."


def test_benchmark_bodies_excluded_from_persona(tmp_path):
    d = _write_agent(tmp_path)
    spec = persona.load_prompt_spec(d)
    # Persona = the NN-prompt.md files only; no benchmark body leaks in.
    assert "Name 5 facts about Germany." not in spec.system
    assert "First by sort order." not in spec.system
    assert "You answer tersely." in spec.system
    assert "{{PWD}}" in spec.system  # raw here; expansion happens in _expand_spec
    # Agent-default max_tokens parsed off the tools manifest.
    assert spec.max_output_tokens == 4096


def _stream_stub(captured_max: list):
    def stub(*, max_output_tokens, **kwargs):
        captured_max.append(max_output_tokens)
        return ModelStreamResult(
            text="ok answer",
            tool_calls=[],
            reasoning="",
            usage=ai.types.usage.Usage(input_tokens=100, output_tokens=20),
            finish_reason="stop",
            assistant_message=ai.assistant_message("ok answer"),
            first_token_s=0.05,
            elapsed_s=0.5,
        )
    return stub


def _bench(agent: str, *, stats_json: str | None = None, stats_csv: str | None = None) -> int:
    return cli._run_bench(
        agent, model=None, reasoning=None, maxout=None, quiet=True, extras=None,
        ignore_local_config=False, ignore_global_config=False, presets=None,
        stats_json=stats_json, stats_csv=stats_csv,
    )


def test_bench_flag_runs_the_named_agent(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(cli, "_run_bench", lambda agent, **kwargs: calls.append((agent, kwargs["quiet"], kwargs["stats_json"])) or 0)

    assert cli.main(["--bench", "jokertest", "-q", "--stats-json", "out.json"]) == 0
    assert calls == [("jokertest", True, "out.json")]


def test_run_bench_writes_stats_and_resolves_max_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_DEFAULT_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_agent(tmp_path)
    (tmp_path / ".js").mkdir(exist_ok=True)
    (tmp_path / ".js" / "jsrc").write_text("set MODEL deepseek/deepseek-v4-flash\n", encoding="utf-8")

    captured: list = []
    monkeypatch.setattr(cli.runtime.model_client, "stream_model_async", _stream_stub(captured))

    out = tmp_path / "stats.json"
    rc = _bench("jokertest", stats_json=str(out))
    assert rc == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["agent"] == "jokertest"
    rows = {r["name"]: r for r in payload["benchmarks"]}
    assert set(rows) == {"01-benchmark", "02-benchmark", "03-benchmark", "04-benchmark"}

    # max_tokens resolution: explicit > -1(None) > agent default(4096).
    assert rows["01-benchmark"]["max_tokens"] == 256
    assert rows["02-benchmark"]["max_tokens"] == 512
    assert rows["03-benchmark"]["max_tokens"] is None
    assert rows["04-benchmark"]["max_tokens"] == 4096
    # …and that those caps actually reached the model layer (order = sorted).
    # -1 is uncapped: no cap is sent, NOT the 4096 agent default.
    assert captured[0] == 256 and captured[1] == 512 and captured[3] == 4096
    assert captured[2] is None

    # Stats math: 20 tok over 0.5s stream = 40 tok/s; ttft surfaced.
    assert rows["02-benchmark"]["output_tokens"] == 20
    assert rows["02-benchmark"]["tok_per_s"] == 40.0
    assert rows["02-benchmark"]["ttft_s"] == 0.05
    assert rows["02-benchmark"]["ok"] is True
    # Bench is throwaway — no session JSONL persisted anywhere.
    assert list((tmp_path / ".js" / "sessions").rglob("*.jsonl")) == []


def test_run_bench_csv_has_one_row_per_benchmark(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_DEFAULT_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_agent(tmp_path)
    monkeypatch.setattr(cli.runtime.model_client, "stream_model_async", _stream_stub([]))

    out = tmp_path / "stats.csv"
    rc = _bench("jokertest", stats_csv=str(out))
    assert rc == 0
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].startswith("name,prompt,max_tokens")
    assert len(lines) == 1 + 4  # header + four benchmarks


def test_bench_requires_benchmark_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_DEFAULT_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    d = tmp_path / ".js" / "agents" / "noBench"
    d.mkdir(parents=True)
    (d / "01-prompt.md").write_text("hi", encoding="utf-8")
    assert _bench("noBench") == 2


def test_failed_benchmark_returns_failure_and_keeps_all_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_agent(tmp_path)

    def fail(**kwargs):
        raise RuntimeError("offline model failure")

    monkeypatch.setattr(cli.runtime.model_client, "stream_model_async", fail)
    out = tmp_path / "stats.json"
    rc = _bench("jokertest", stats_json=str(out))

    rows = json.loads(out.read_text(encoding="utf-8"))["benchmarks"]
    assert len(rows) == 4
    assert all(row["ok"] is False for row in rows)
    assert rc == 1


def test_bench_tools_keep_telemetry_order_and_unknown_marker(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_agent(tmp_path)
    async def turn(cfg, system, messages, telemetry, **kw):
        assert kw["trace_override"] is True
        assert cfg.settings["REASONING_DISPLAY"] == 3
        assert cfg.settings["TOOLS_DISPLAY"] == 3
        assert cfg.settings["MARKDOWN"] is False
        for kind, name in [("tool_ok", "read"), ("tool_error", "write"),
                           ("tool_exception", "write"), ("tool_unknown", "shell"),
                           ("tool_invalid", "invalid"), ("tool_call_refused", "refused")]:
            telemetry.event(kind, tool=name)
        print("visible tool results")
        messages.append({"role": "assistant", "content": "answer"})
    monkeypatch.setattr(cli.runtime, "run_turn_async", turn)
    assert cli.main(["--bench", "jokertest"]) == 0
    out = capsys.readouterr().out
    assert "visible tool results" in out
    payload = json.loads(out[out.index('{'):])
    row = payload["benchmarks"][0]
    assert row["tools"] == ["read", "write", "write", "?shell", "invalid", "refused"]
    assert {"agent", "model", "provider", "benchmarks"} == payload.keys()
    assert {"name", "prompt", "max_tokens", "ok", "error", "calls", "prompt_tokens", "output_tokens",
            "cached_tokens", "ttft_s", "stream_s", "tok_per_s", "wall_s", "finish_reason", "tools"} == row.keys()


def test_plain_bench_json_and_json_stream_share_payload(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_agent(tmp_path)
    monkeypatch.setattr(cli.runtime.model_client, "stream_model_async", _stream_stub([]))
    assert cli.main(["--bench", "jokertest"]) == 0
    raw = capsys.readouterr().out
    plain = json.loads(raw[raw.rindex("\n{") + 1:])
    assert cli.main(["--bench", "jokertest", "--json"]) == 0
    final = json.loads(capsys.readouterr().out)
    assert cli.main(["--bench", "jokertest", "--json-stream"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "start"
    end = events[-1]
    assert end.pop("type") == "end"
    for payload in (plain, final, end):
        for row in payload["benchmarks"]:
            row.pop("wall_s")
    assert plain == final == end


def test_bench_parallel_calls_keep_model_order_and_full_output(tmp_path, monkeypatch, capsys):
    import threading
    from js import runtime
    from js.model_client import ModelToolCall
    from js.toolkit.core import Tool, ToolContext
    from js.toolkit.registry import ToolRegistry

    monkeypatch.chdir(tmp_path)
    agent = _write_agent(tmp_path)
    for path in agent.glob("*-benchmark.md"):
        if path.name != "01-benchmark.md":
            path.unlink()
    (agent / "agent.yaml").write_text("tools: ['*:eager']\n")
    finished = threading.Event()
    completed = []
    def slow(context):
        assert finished.wait(5)
        completed.append("slow")
        return "\n".join(f"full-output-{i}" for i in range(40))
    def fast(context):
        completed.append("fast")
        finished.set()
        return "fast output"
    registry = ToolRegistry((Tool("slow", "slow", slow, {}, aliases=("slow_alias",), read_only=True),
                             Tool("fast", "fast", fast, {}, read_only=True)), {"slow_alias": "slow"})
    monkeypatch.setattr(run_assembly, "registry_for", lambda cfg: registry)
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))
    calls = 0
    def stream(**kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            tool_calls = [ModelToolCall(id="s", name="slow_alias", arguments="{}"),
                          ModelToolCall(id="f", name="fast", arguments="{}")]
            return ModelStreamResult(
                text="", tool_calls=tool_calls, reasoning="", usage=None, finish_reason="tool_calls",
                assistant_message=ai.types.messages.Message(role="assistant", parts=[
                    ai.types.messages.ToolCallPart(tool_call_id=c.id, tool_name=c.name, tool_args=c.arguments)
                    for c in tool_calls]),
            )
        kw["on_reasoning"]("full bench reasoning")
        kw["on_text"]("**raw markdown**")
        return ModelStreamResult(text="**raw markdown**", tool_calls=[], reasoning="full bench reasoning", usage=None,
                                 finish_reason="stop", assistant_message=ai.assistant_message("**raw markdown**"))
    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    assert cli.main(["--bench", "jokertest", "--extra", "PARALLEL_TOOLS_MAX=2",
                     "--extra", "REASONING_DISPLAY=0", "--extra", "TOOLS_DISPLAY=0",
                     "--extra", "TOOLS_PREVIEW_LINES=1", "--extra", "MARKDOWN=on"]) == 0
    captured = capsys.readouterr()
    assert completed == ["fast", "slow"]
    assert "full-output-39" in captured.out
    assert "**raw markdown**" in captured.out
    assert "full bench reasoning" in captured.err
    payload = json.loads(captured.out[captured.out.rindex('\n{') + 1:])
    assert payload["benchmarks"][0]["tools"] == ["slow_alias", "fast"]
