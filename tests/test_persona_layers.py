"""Which layer supplies the system prompt, manifest, seeds and benchmarks
(ALL-CLAIMS persona.py:241-249 HIGH, :125-127 LOW)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from js import persona


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
    repo, global_root, project = layers
    return persona.load_agent_prompt_spec(
        "x", repo_prompts_root=repo, global_agents_root=global_root, project_agents_root=project)


def _cfg(layers):
    return SimpleNamespace(agent_id="x", prompt_roots=layers, agents_files=())


@pytest.mark.parametrize("name", ["01-benchmark.md", "benchmark.md", "01-user.md", "01-agent.md"])
def test_a_layer_without_persona_text_does_not_blank_the_system_prompt(layers, name):
    (layers[2] / "x" / name).write_text("PROJECT ONLY\n", encoding="utf-8")
    spec = _spec(layers)
    assert spec.system == "LOWER SYSTEM\n"
    assert spec.model == "lower-model"
    assert spec.tool_selectors == ("read:eager",)


def test_seed_only_layer_keeps_its_seed_over_the_lower_one(layers):
    (layers[2] / "x" / "01-user.md").write_text("PROJECT SEED\n", encoding="utf-8")
    assert [e.user for e in _spec(layers).exchanges] == ["PROJECT SEED\n"]


def test_benchmark_only_layer_is_still_where_bench_reads(layers):
    (layers[2] / "x" / "01-benchmark.md").write_text("PROJECT BENCH\n", encoding="utf-8")
    chosen = persona.resolve_agent_prompt_dir(_cfg(layers))
    assert [b.prompt for b in persona.load_benchmarks(chosen)] == ["PROJECT BENCH"]


def test_zero_benchmark_with_frontmatter_is_a_benchmark_not_a_legacy_manifest(layers):
    (layers[2] / "x" / "00-benchmark.md").write_text("---\nmax_tokens: 42\n---\nPROJECT BENCH\n", encoding="utf-8")
    assert _spec(layers).system == "LOWER SYSTEM\n"
    chosen = persona.resolve_agent_prompt_dir(_cfg(layers))
    assert [(b.prompt, b.max_tokens) for b in persona.load_benchmarks(chosen)] == [("PROJECT BENCH", 42)]


def test_project_wording_override_still_wins(layers):
    (layers[2] / "x" / "01-prompt.md").write_text("PROJECT SYSTEM\n", encoding="utf-8")
    spec = _spec(layers)
    assert spec.system == "PROJECT SYSTEM\n" and spec.model == "lower-model"


def test_global_persona_text_beats_repo_under_a_seed_only_project(layers):
    (layers[1] / "x" / "01-prompt.md").write_text("GLOBAL SYSTEM\n", encoding="utf-8")
    (layers[2] / "x" / "01-user.md").write_text("PROJECT SEED\n", encoding="utf-8")
    assert _spec(layers).system == "GLOBAL SYSTEM\n"


def test_an_explicit_manifest_alone_means_an_empty_system_prompt(layers):
    (layers[2] / "x" / "agent.yaml").write_text("model: intentional-model\n", encoding="utf-8")
    spec = _spec(layers)
    assert spec.model == "intentional-model" and spec.system.strip() == ""
