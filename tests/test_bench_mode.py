"""--bench mode: persona benchmark loading, max_tokens resolution, stats emit."""

from __future__ import annotations

import json
from pathlib import Path

import ai
import ai.types.usage

from js import cli, persona
from js.model_client import ModelStreamResult


def _write_agent(root: Path) -> Path:
    """A joker-shaped agent dir: persona prompts + a tools manifest with a
    max_tokens default + four benchmarks exercising every max_tokens case."""
    d = root / ".js" / "agents" / "jokertest"
    d.mkdir(parents=True)
    (d / "00-tools.yaml").write_text("max_tokens: 4096\ntools:\n  - read\n", encoding="utf-8")
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


def test_run_bench_writes_stats_and_resolves_max_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_agent(tmp_path)

    captured: list = []
    monkeypatch.setattr(cli.runtime.model_client, "stream_model", _stream_stub(captured))

    out = tmp_path / "stats.json"
    rc = cli.main(["--bench", "jokertest", "-q", "--stats-json", str(out)])
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
    # -1 is uncapped: run_turn turns a None cap into the model's own resolved
    # max — NOT the 4096 agent default — so the wire value is large, not None.
    assert captured[0] == 256 and captured[1] == 512 and captured[3] == 4096
    assert captured[2] not in (None, 4096) and captured[2] > 4096

    # Stats math: 20 tok over 0.5s stream = 40 tok/s; ttft surfaced.
    assert rows["02-benchmark"]["output_tokens"] == 20
    assert rows["02-benchmark"]["tok_per_s"] == 40.0
    assert rows["02-benchmark"]["ttft_s"] == 0.05
    assert rows["02-benchmark"]["ok"] is True
    # Bench is throwaway — no session JSONL persisted anywhere.
    assert list((tmp_path / ".local" / "share" / "js" / "sessions").rglob("*.jsonl")) == []


def test_run_bench_csv_has_one_row_per_benchmark(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_agent(tmp_path)
    monkeypatch.setattr(cli.runtime.model_client, "stream_model", _stream_stub([]))

    out = tmp_path / "stats.csv"
    rc = cli.main(["--bench", "jokertest", "-q", "--stats-csv", str(out)])
    assert rc == 0
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].startswith("name,prompt,max_tokens")
    assert len(lines) == 1 + 4  # header + four benchmarks


def test_bench_requires_benchmark_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    d = tmp_path / ".js" / "agents" / "noBench"
    d.mkdir(parents=True)
    (d / "01-prompt.md").write_text("hi", encoding="utf-8")
    rc = cli.main(["--bench", "noBench", "-q"])
    assert rc == 2
    assert "no NN-benchmark.md" in capsys.readouterr().err
