from __future__ import annotations

from pathlib import Path

import pytest

from js import persona
from js import runtime
from js.config import Config
from js.model_client import ModelStreamResult
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry, select
import ai
import ai.types.usage


def _fake_stream_result(text: str = "ok") -> ModelStreamResult:
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )




def cfg(tmp_path: Path, prompts: Path) -> Config:
    return Config(
        agent_id="surface-agent",
        agent_dir=tmp_path / ".js" / "sessions" / "surface-agent",
        model="offline-test-model",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / "surface-agent",
        session_file=tmp_path / ".js" / "sessions" / "surface-agent" / "surface.jsonl",
        prompts_dir=prompts,
    )


def write_prompt_dir(
    tmp_path: Path,
    zero: str | None,
    *rest: tuple[str, str],
    zero_name: str = "agent.yaml",
) -> Path:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True)
    if zero is not None:
        (prompts / zero_name).write_text(zero, encoding="utf-8")
    for name, text in rest:
        (prompts / name).write_text(text, encoding="utf-8")
    return prompts


def names(registry) -> list[str]:
    return [tool.name for tool in registry.tools]


def test_registry_selection_handles_empty_globs_aliases_unknowns_and_dedupe():
    full = build_default_registry()

    assert names(select([])) == []
    assert names(select(["*:lazy"])) == names(full)

    fs_names = set(names(select(["fs_*:lazy"])))
    assert fs_names == {"fs_search"}

    assert names(select(["wiki_*:lazy"])) == ["wiki_convert", "wiki_write", "wiki_finish_ingest"]
    assert names(select(["grep:lazy"])) == []
    assert names(select(["read:lazy", "Read:lazy", "fs_read:lazy", "unknown:lazy", "read:lazy"])) == ["read"]
    # prompt-dir agents are selectable by name and reachable via a prefix glob
    # (discovered from prompts/, so adding/removing an agent dir never snaps this).
    prompt_agents = sorted(p.name for p in Path("prompts").iterdir() if p.is_dir())
    assert prompt_agents, "prompts/ should expose at least one agent dir"
    assert names(select([f"{name}:eager" for name in prompt_agents])) == prompt_agents
    assert prompt_agents[0] in names(select([prompt_agents[0][:-2] + "*:eager"]))


def test_yaml_tools_manifest_is_parsed_and_not_prompt_body(tmp_path):
    prompts = write_prompt_dir(
        tmp_path,
        (
            "tools:\n"
            "  - wiki_*:lazy\n"
            "model: primary-model\n"
            "secondary_model: backup-model\n"
            "sampling:\n"
            "  temperature: 0.2\n"
        ),
        ("01-first.md", "FIRST\n"),
        ("02-second.md", "SECOND\n"),
    )

    spec = persona.load_prompt_spec(prompts)

    assert spec.tool_selectors == ("wiki_*:lazy",)
    assert spec.model == "primary-model"
    assert spec.secondary_model == "backup-model"
    assert spec.sampling == {"temperature": 0.2}
    assert spec.system == "FIRST\n\nSECOND\n"


@pytest.mark.parametrize(("name", "text"), [
    ("00-tools.yaml", "tools:\n  - read\n"),
    ("00-tools.md", "---\ntools:\n  - shell\n---\nLEGACY BODY\n"),
])
def test_pre_agent_yaml_manifests_are_refused_naming_agent_yaml(tmp_path, name, text):
    prompts = write_prompt_dir(tmp_path, text, ("01.md", "BODY\n"), zero_name=name)
    (prompts / "agent.yaml").write_text("tools: [plan:eager]\n", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        persona.load_prompt_spec(prompts)

    message = str(excinfo.value)
    assert "\n" not in message
    assert name in message and "agent.yaml" in message


def test_zero_markdown_without_frontmatter_is_prompt_text(tmp_path):
    prompts = write_prompt_dir(tmp_path, "HEAD\n", ("01.md", "BODY\n"), zero_name="00-intro.md")

    assert persona.load_prompt_spec(prompts).system == "HEAD\n\nBODY\n"


def test_agent_yaml_with_prompt_md_loads_model_reasoning_tools_and_skills(tmp_path):
    prompts = write_prompt_dir(
        tmp_path,
        "model: cpa/claude-fable-5-1\nreasoning: high\ntools:\n  - tag:read_only\n  - shell:ban\n"
        "skills:\n  - engineering:*\n",
        ("prompt.md", "PROMPT\n"),
    )

    spec = persona.load_prompt_spec(prompts)

    assert spec.model == "cpa/claude-fable-5-1"
    assert spec.reasoning_effort == "high"
    assert spec.tool_selectors == ("tag:read_only", "shell:ban")
    assert spec.skills == ("engineering:*",)
    assert spec.system == "PROMPT\n"
    registry = build_default_registry().select(spec.tool_selectors)
    assert "read" in registry.by_name and "shell" not in registry.by_name
    assert not registry.lazy


def test_agent_yaml_unknown_key_is_refused(tmp_path):
    prompts = write_prompt_dir(tmp_path, "reasoning_effort: high\n", ("prompt.md", "PROMPT\n"))

    with pytest.raises(ValueError, match="reasoning_effort"):
        persona.load_prompt_spec(prompts)


def test_yaml_manifest_absent_tools_empty_list_and_missing_00_default_none(tmp_path):
    manifest_only = write_prompt_dir(tmp_path / "manifest", "tools: []\n", ("01.md", "BODY\n"))
    no_tools_key = write_prompt_dir(tmp_path / "nokey", "model: x\n", ("01.md", "BODY\n"))
    missing_zero = write_prompt_dir(tmp_path / "missing", None, ("01.md", "BODY\n"))

    assert persona.load_prompt_spec(manifest_only).tool_selectors == ()
    assert persona.load_prompt_spec(manifest_only).system == "BODY\n"
    assert persona.load_prompt_spec(no_tools_key).tool_selectors == ()
    assert persona.load_prompt_spec(missing_zero).tool_selectors == ()


def test_yaml_manifest_malformed_yaml_fails_clear(tmp_path):
    prompts = write_prompt_dir(tmp_path, "tools: [\n", ("01.md", "BODY\n"))

    with pytest.raises(ValueError):
        persona.load_prompt_spec(prompts)


def test_project_dir_missing_manifest_falls_back_to_lower_layer_manifest(tmp_path):
    """A project agent dir that only overrides the prompt wording (no
    agent.yaml of its own) must not silently boot with zero tools — it
    should inherit the nearest lower layer's manifest instead."""
    repo_root = tmp_path / "prompts"
    global_root = tmp_path / "global-agents"
    project_root = tmp_path / "project-agents"
    repo_root.mkdir()
    global_root.mkdir()
    project_root.mkdir()

    (repo_root / "myagent").mkdir()
    (repo_root / "myagent" / "agent.yaml").write_text(
        "tools:\n  - wiki_*:lazy\nmodel: repo-model\n", encoding="utf-8"
    )
    (repo_root / "myagent" / "01-prompt.md").write_text("REPO PROMPT\n", encoding="utf-8")

    (project_root / "myagent").mkdir()
    (project_root / "myagent" / "01-prompt.md").write_text("PROJECT PROMPT\n", encoding="utf-8")

    spec = persona.load_agent_prompt_spec(
        "myagent",
        repo_prompts_root=repo_root,
        global_agents_root=global_root,
        project_agents_root=project_root,
    )

    assert spec.system == "PROJECT PROMPT\n"
    assert spec.tool_selectors == ("wiki_*:lazy",)
    assert spec.model == "repo-model"


def test_project_dir_with_explicit_empty_manifest_is_not_overridden_by_fallback(tmp_path):
    """An explicit `tools: []` in the winning dir is a deliberate choice, not
    accidental shadowing — the fallback must leave it alone."""
    repo_root = tmp_path / "prompts"
    global_root = tmp_path / "global-agents"
    project_root = tmp_path / "project-agents"
    repo_root.mkdir()
    global_root.mkdir()
    project_root.mkdir()

    (repo_root / "myagent").mkdir()
    (repo_root / "myagent" / "agent.yaml").write_text("tools:\n  - wiki_*:lazy\n", encoding="utf-8")
    (repo_root / "myagent" / "01-prompt.md").write_text("REPO PROMPT\n", encoding="utf-8")

    (project_root / "myagent").mkdir()
    (project_root / "myagent" / "agent.yaml").write_text("tools: []\n", encoding="utf-8")
    (project_root / "myagent" / "01-prompt.md").write_text("PROJECT PROMPT\n", encoding="utf-8")

    spec = persona.load_agent_prompt_spec(
        "myagent",
        repo_prompts_root=repo_root,
        global_agents_root=global_root,
        project_agents_root=project_root,
    )

    assert spec.system == "PROJECT PROMPT\n"
    assert spec.tool_selectors == ()


def test_runtime_omits_tools_when_agent_selection_is_empty(monkeypatch, tmp_path):
    prompts = write_prompt_dir(tmp_path, None, ("01.md", "SYSTEM\n"))
    calls: list[dict] = []

    def stream_stub(**kwargs):
        calls.append(kwargs)
        return _fake_stream_result("NO_TOOLS_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream_stub)
    messages = [{"role": "user", "content": "hi"}]

    runtime.run_turn(
        cfg(tmp_path, prompts),
        persona.load_prompt(prompts),
        messages,
        runtime.Telemetry(None),
        tool_registry=select([]),
        trace_override=False,
    )

    assert calls[0].get("tools") is None
    assert messages[-1] == {"role": "assistant", "content": "NO_TOOLS_OK"}


def test_runtime_dispatch_rejects_unselected_tool_cleanly(tmp_path):
    registry = select(["plan:lazy"])
    (tmp_path / "note.txt").write_text("unselected tool must not read this", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    _, result = runtime._dispatch(
        "read",
        '{"file_path":"note.txt"}',
        runtime.Telemetry(None),
        cap_bytes=4096,
        registry=registry,
        tool_context=context,
    )

    assert result.startswith("ERROR:")
    assert "read" in result
    assert "unselected tool must not read this" not in result
    assert context.read_paths == set()
