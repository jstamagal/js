"""00-tools.yaml / 00*.md frontmatter -> agent.yaml migration, on tmp copies."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

from js import persona

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_agents.py"
_spec = importlib.util.spec_from_file_location("migrate_agents", _SCRIPT)
agent_migrate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = agent_migrate  # dataclasses resolve their module by name
_spec.loader.exec_module(agent_migrate)

OLD_MANIFEST = """\
# builder agent: writes code
model: omni/qwen-free-random   # cheap
reasoning_effort: xhigh
max_tokens: 8192
tools:
  # files
  - read
  - write
  - todo_write
  - todo_*
  # delegation
  - wiki_*
  - reviewer
sampling:
  temperature: 0.2
"""


def _agent(root: Path, name: str, files: dict[str, str]) -> Path:
    agent = root / name
    agent.mkdir(parents=True)
    for filename, text in files.items():
        (agent / filename).write_text(text, encoding="utf-8")
    return agent


def test_dry_run_changes_nothing(tmp_path):
    agent = _agent(tmp_path, "builder", {"00-tools.yaml": OLD_MANIFEST, "01-prompt.md": "BUILD\n"})

    result = agent_migrate.migrate_agent_dir(agent)

    assert result.action == "migrate"
    assert (agent / "00-tools.yaml").read_text(encoding="utf-8") == OLD_MANIFEST
    assert not (agent / "agent.yaml").exists()


def test_apply_writes_agent_yaml_and_the_agent_loads_the_same_settings(tmp_path):
    agent = _agent(tmp_path, "builder", {"00-tools.yaml": OLD_MANIFEST, "01-prompt.md": "BUILD\n"})
    _agent(tmp_path, "reviewer", {"01-prompt.md": "REVIEW\n"})

    result = agent_migrate.migrate_agent_dir(agent, apply=True)

    assert result.action == "migrate"
    # todo_write names no tool and drops; the todo_* glob is kept (a glob that
    # matches nothing is harmless at load and may match a later tool).
    assert sorted(result.dropped) == ["todo_write"]
    assert not (agent / "00-tools.yaml").exists()
    spec = persona.load_prompt_spec(agent)
    assert spec.model == "omni/qwen-free-random"
    assert spec.reasoning_effort == "xhigh"
    assert spec.max_output_tokens == 8192
    assert spec.sampling == {"temperature": 0.2}
    assert spec.tool_selectors == ("read:eager", "write:eager", "todo_*:eager", "wiki_*:eager", "reviewer:eager")
    assert spec.system == "BUILD\n"
    text = (agent / "agent.yaml").read_text(encoding="utf-8")
    assert "# builder agent: writes code" in text and "# delegation" in text


def test_column_zero_list_and_flow_list_both_migrate(tmp_path):
    col0 = _agent(tmp_path, "col0", {"00-tools.yaml": "tools:\n- read\n- shell\n", "01.md": "X\n"})
    flow = _agent(tmp_path, "flow", {"00-tools.yaml": "tools: [read, \"*\"]\n", "01.md": "X\n"})

    for agent in (col0, flow):
        assert agent_migrate.migrate_agent_dir(agent, apply=True).action == "migrate"

    assert persona.load_prompt_spec(col0).tool_selectors == ("read:eager", "shell:eager")
    assert persona.load_prompt_spec(flow).tool_selectors == ("read:eager", "*:eager")


def test_frontmatter_zero_markdown_moves_to_agent_yaml_and_keeps_its_body(tmp_path):
    agent = _agent(tmp_path, "legacy", {
        "00-tools.md": "---\ntools:\n  - shell\nmodel: m1\n---\nLEGACY BODY\n",
        "01.md": "BODY\n",
    })

    assert agent_migrate.migrate_agent_dir(agent, apply=True).action == "migrate"

    spec = persona.load_prompt_spec(agent)
    assert spec.tool_selectors == ("shell:eager",)
    assert spec.model == "m1"
    assert spec.system == "LEGACY BODY\n\nBODY\n"


def test_existing_agent_yaml_and_bad_yaml_are_left_alone(tmp_path):
    both = _agent(tmp_path, "both", {"00-tools.yaml": "tools: [read]\n", "agent.yaml": "tools: []\n", "01.md": "X\n"})
    broken = _agent(tmp_path, "broken", {"00-tools.yaml": "omni/x\nreasoning_effort: high\n", "01.md": "X\n"})

    assert agent_migrate.migrate_agent_dir(both, apply=True).action == "skip"
    assert agent_migrate.migrate_agent_dir(broken, apply=True).action == "error"

    assert (both / "00-tools.yaml").exists() and yaml.safe_load((both / "agent.yaml").read_text()) == {"tools": []}
    assert (broken / "00-tools.yaml").exists() and not (broken / "agent.yaml").exists()


def test_main_walks_a_root_skips_symlinks_and_only_writes_with_apply(tmp_path, capsys):
    root = tmp_path / "agents"
    agent = _agent(root, "builder", {"00-tools.yaml": "tools: [read]\n", "01.md": "X\n"})
    elsewhere = _agent(tmp_path / "remote", "shared", {"00-tools.yaml": "tools: [read]\n", "01.md": "X\n"})
    (root / "shared").symlink_to(elsewhere)

    assert agent_migrate.main([str(root)]) == 0
    assert (agent / "00-tools.yaml").exists()

    assert agent_migrate.main(["--apply", str(root)]) == 0
    assert (agent / "agent.yaml").exists() and not (agent / "00-tools.yaml").exists()
    assert (elsewhere / "00-tools.yaml").exists() and not (elsewhere / "agent.yaml").exists()


def test_entries_that_match_no_tool_are_dropped_and_the_rest_kept(tmp_path):
    root = tmp_path / "agents"
    agent = _agent(root, "builder", {"00-tools.yaml": (
        "tools:\n  - read\n  - multi_patch\n  - sibling\n  - wiki_*\n  - artifact_*\n"
        "  - tag:read_only\n  - shell:ban\n"), "01-prompt.md": "X\n"})
    _agent(root, "sibling", {"01-prompt.md": "S\n"})

    result = agent_migrate.migrate_agent_dir(agent, apply=True)

    assert result.action == "migrate"
    assert sorted(result.dropped) == ["multi_patch"]
    assert persona.load_prompt_spec(agent).tool_selectors == (
        "read:eager", "sibling:eager", "wiki_*:eager", "artifact_*:eager", "tag:read_only", "shell:ban")


def test_an_existing_agent_yaml_loses_only_its_dead_entries(tmp_path, capsys):
    root = tmp_path / "agents"
    text = "# keep me\nmodel: m1\ntools:\n  - read:eager  # files\n  - grep:eager\n  - sem_search:lazy\n"
    agent = _agent(root, "old", {"agent.yaml": text, "01.md": "X\n"})
    clean = _agent(root, "clean", {"agent.yaml": "tools:\n  - read:eager\n", "01.md": "X\n"})

    assert agent_migrate.main([str(root)]) == 0
    assert (agent / "agent.yaml").read_text(encoding="utf-8") == text

    assert agent_migrate.main(["--apply", str(root)]) == 0

    rewritten = (agent / "agent.yaml").read_text(encoding="utf-8")
    assert "# keep me" in rewritten and "# files" in rewritten
    assert persona.load_prompt_spec(agent).tool_selectors == ("read:eager",)
    assert persona.load_prompt_spec(agent).model == "m1"
    assert (clean / "agent.yaml").read_text(encoding="utf-8") == "tools:\n  - read:eager\n"
    out = capsys.readouterr().out
    assert "grep" in out and "sem_search" in out


from js import agent_migration


def _keep_all(_entry):
    return True


def test_message_and_benchmark_frontmatter_are_content_not_manifests(tmp_path):
    """ALL-CLAIMS:18 + tuck #138: 00-user/00-agent/00-benchmark keep their frontmatter."""
    for name, head in (("00-user.md", "model: user-text"), ("00-agent.md", "model: seed"),
                       ("00-benchmark.md", "max_tokens: 42")):
        text = f"---\n{head}\n---\nBODY\n"
        agent = _agent(tmp_path, name.removesuffix(".md"), {name: text, "01.md": "X\n"})
        result = agent_migration.migrate_agent_dir(agent, apply=True, keep=_keep_all)
        assert result.action == "none", name
        assert (agent / name).read_text(encoding="utf-8") == text
        assert not (agent / "agent.yaml").exists()


def test_legacy_yaml_beside_message_and_benchmark_files_migrates(tmp_path):
    """ALL-CLAIMS:19: a message file is not a competing manifest."""
    bench = "---\nmax_tokens: 42\n---\nbench\n"
    agent = _agent(tmp_path, "legacy", {
        "00-tools.yaml": "tools: [read]\n", "00-user.md": "seed\n", "00-benchmark.md": bench})
    result = agent_migration.migrate_agent_dir(agent, apply=True, keep=_keep_all)
    assert result.action == "migrate"
    assert not (agent / "00-tools.yaml").exists()
    assert (agent / "00-user.md").read_text(encoding="utf-8") == "seed\n"
    assert (agent / "00-benchmark.md").read_text(encoding="utf-8") == bench


def test_undecodable_agent_is_an_error_and_later_agents_still_migrate(tmp_path):
    """ALL-CLAIMS:20: one non-UTF-8 file must not kill the whole root."""
    root = tmp_path / "agents"
    bad = _agent(root, "a_bad", {"01.md": "X\n"})
    (bad / "00-tools.yaml").write_bytes(b"tools: [\xff]\n")
    good = _agent(root, "b_good", {"00-tools.yaml": "tools: [read]\n", "01.md": "X\n"})
    results = agent_migration.migrate_root(root, apply=True, keep=_keep_all)
    assert [r.action for r in results] == ["error", "migrate"]
    assert "00-tools.yaml" in results[0].message
    assert (bad / "00-tools.yaml").read_bytes() == b"tools: [\xff]\n"
    assert (good / "agent.yaml").exists()


def _refuse(monkeypatch, target, *, attr="replace"):
    import os

    real = getattr(os, attr) if attr == "replace" else None

    if attr == "replace":
        def refuse(src, dst, *a, **k):
            if os.fspath(dst) == os.fspath(target):
                raise OSError("injected replace failure")
            return real(src, dst, *a, **k)
        monkeypatch.setattr(os, "replace", refuse)
    else:
        real_unlink = Path.unlink

        def refuse(self, *a, **k):
            if self == target:
                raise OSError("injected unlink failure")
            return real_unlink(self, *a, **k)
        monkeypatch.setattr(Path, "unlink", refuse)


def test_failed_manifest_write_leaves_the_legacy_agent_and_a_rerun_finishes(tmp_path, monkeypatch):
    agent = _agent(tmp_path, "fault", {"00-tools.yaml": "tools: [read]\n", "01.md": "X\n"})
    with monkeypatch.context() as m:
        _refuse(m, agent / "agent.yaml")
        assert agent_migration.migrate_agent_dir(agent, apply=True, keep=_keep_all).action == "error"
    assert (agent / "00-tools.yaml").read_text(encoding="utf-8") == "tools: [read]\n"
    assert sorted(p.name for p in agent.iterdir()) == ["00-tools.yaml", "01.md"]
    assert agent_migration.migrate_agent_dir(agent, apply=True, keep=_keep_all).action == "migrate"
    assert (agent / "agent.yaml").exists() and not (agent / "00-tools.yaml").exists()


def test_failed_legacy_removal_takes_agent_yaml_back_out(tmp_path, monkeypatch):
    """ALL-CLAIMS:22: a crash between writes must not leave `skip BOTH` forever."""
    agent = _agent(tmp_path, "fault", {"00-tools.yaml": "tools: [read]\n", "01.md": "X\n"})
    with monkeypatch.context() as m:
        _refuse(m, agent / "00-tools.yaml", attr="unlink")
        assert agent_migration.migrate_agent_dir(agent, apply=True, keep=_keep_all).action == "error"
    assert (agent / "00-tools.yaml").read_text(encoding="utf-8") == "tools: [read]\n"
    assert not (agent / "agent.yaml").exists()
    assert agent_migration.migrate_agent_dir(agent, apply=True, keep=_keep_all).action == "migrate"


def test_failed_body_rewrite_keeps_the_markdown_whole(tmp_path, monkeypatch):
    original = "---\nmodel: m\n---\nBODY MUST STAY\n"
    agent = _agent(tmp_path, "body", {"00-tools.md": original})
    with monkeypatch.context() as m:
        _refuse(m, agent / "00-tools.md")
        assert agent_migration.migrate_agent_dir(agent, apply=True, keep=_keep_all).action == "error"
    assert (agent / "00-tools.md").read_text(encoding="utf-8") == original
    assert not (agent / "agent.yaml").exists()
    assert agent_migration.migrate_agent_dir(agent, apply=True, keep=_keep_all).action == "migrate"
    assert (agent / "00-tools.md").read_text(encoding="utf-8") == "BODY MUST STAY\n"


def test_half_finished_run_with_matching_agent_yaml_is_finished(tmp_path):
    yaml_text = "# mine\ntools:\n  - read:eager\nmodel: m\n"
    agent = _agent(tmp_path, "both", {
        "00-tools.md": "---\nmodel: m\ntools: [read]\n---\nBODY MUST STAY\n", "agent.yaml": yaml_text})
    assert agent_migration.migrate_agent_dir(agent, apply=True, keep=_keep_all).action == "migrate"
    assert (agent / "00-tools.md").read_text(encoding="utf-8") == "BODY MUST STAY\n"
    assert (agent / "agent.yaml").read_text(encoding="utf-8") == yaml_text


def test_both_with_different_settings_is_left_alone(tmp_path):
    files = {"00-tools.md": "---\nmodel: old\n---\nOLD BODY\n", "agent.yaml": "model: new\n"}
    agent = _agent(tmp_path, "conflict", files)
    assert agent_migration.migrate_agent_dir(agent, apply=True, keep=_keep_all).action == "skip"
    for name, text in files.items():
        assert (agent / name).read_text(encoding="utf-8") == text


def test_both_finish_failing_restores_the_existing_agent_yaml_bytes(tmp_path, monkeypatch):
    yaml_text = "tools:\n  - read:eager\n"
    agent = _agent(tmp_path, "both", {"00-tools.yaml": "tools: [read]\n", "agent.yaml": yaml_text})
    with monkeypatch.context() as m:
        _refuse(m, agent / "00-tools.yaml", attr="unlink")
        assert agent_migration.migrate_agent_dir(agent, apply=True, keep=_keep_all).action == "error"
    assert (agent / "agent.yaml").read_text(encoding="utf-8") == yaml_text
    assert (agent / "00-tools.yaml").exists()


def test_mcp_globs_and_project_agents_survive_a_partial_registry(tmp_path, monkeypatch):
    """ALL-CLAIMS:21: entries the registry cannot see are not proven dead."""
    project = tmp_path / "project"
    _agent(project / ".js" / "agents", "local_helper", {"01-prompt.md": "H\n"})
    monkeypatch.chdir(project)
    root = tmp_path / "agents"
    text = ("tools:\n  - read:eager\n  - mcp__*:lazy\n  - mcp__files__read:eager\n"
            "  - future_*:lazy\n  - local_helper:eager\n  - future_agent:eager\n")
    agent = _agent(root, "user", {"agent.yaml": text})
    keep = agent_migration.tool_matcher(agent_migration.default_roots(root))
    result = agent_migration.migrate_agent_dir(agent, apply=True, keep=keep)
    assert result.dropped == ["future_agent:eager"]
    assert (agent / "agent.yaml").read_text(encoding="utf-8") == text.replace("  - future_agent:eager\n", "")
