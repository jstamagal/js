"""Which layer supplies the system prompt, manifest, seeds and benchmarks."""
from __future__ import annotations

import pytest

from js import persona
from js.config import Config


@pytest.fixture
def layers(tmp_path):
    repo, global_root, project = (tmp_path / name for name in ("repo", "global", "project"))
    for root in (repo, global_root, project):
        (root / "x").mkdir(parents=True)
    (repo / "x" / "agent.yaml").write_text("model: lower-model\ntools: [read:eager]\n", encoding="utf-8")
    (repo / "x" / "01-prompt.md").write_text("LOWER SYSTEM\n", encoding="utf-8")
    (repo / "x" / "01-user.md").write_text("LOWER SEED\n", encoding="utf-8")
    return repo, global_root, project


def _spec(layers):
    return persona.load_agent_prompt_spec("x", layers)


def _cfg(layers):
    return Config(model="m", agent_id="x", prompt_roots=layers)


def test_a_nearer_agent_directory_replaces_the_lower_one_whole(layers):
    (layers[2] / "x" / "agent.yaml").write_text("model: intentional-model\n", encoding="utf-8")
    spec = _spec(layers)
    assert spec.model == "intentional-model"
    assert spec.system == "\n"
    assert spec.tool_selectors == ()
    assert spec.exchanges == ()


def test_seed_only_layer_keeps_its_seed_over_the_lower_one(layers):
    (layers[2] / "x" / "01-user.md").write_text("PROJECT SEED\n", encoding="utf-8")
    assert [e.user for e in _spec(layers).exchanges] == ["PROJECT SEED\n"]


def test_benchmark_only_layer_is_still_where_bench_reads(layers):
    (layers[2] / "x" / "01-benchmark.md").write_text("PROJECT BENCH\n", encoding="utf-8")
    chosen = _cfg(layers).prompts_dir
    assert [b.prompt for b in persona.load_benchmarks(chosen)] == ["PROJECT BENCH"]


def test_zero_benchmark_with_frontmatter_is_a_benchmark_not_a_legacy_manifest(layers):
    (layers[2] / "x" / "00-benchmark.md").write_text("---\nmax_tokens: 42\n---\nPROJECT BENCH\n", encoding="utf-8")
    assert _spec(layers).system == "\n"
    chosen = _cfg(layers).prompts_dir
    assert [(b.prompt, b.max_tokens) for b in persona.load_benchmarks(chosen)] == [("PROJECT BENCH", 42)]


def test_project_wording_override_still_wins(layers):
    (layers[2] / "x" / "01-prompt.md").write_text("PROJECT SYSTEM\n", encoding="utf-8")
    spec = _spec(layers)
    assert spec.system == "PROJECT SYSTEM\n" and spec.model == ""
