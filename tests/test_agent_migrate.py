"""00-tools.yaml / 00*.md frontmatter -> agent.yaml migration, on tmp copies."""
from __future__ import annotations

from pathlib import Path

import yaml

from js import agent_migrate, persona

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

    result = agent_migrate.migrate_agent_dir(agent, apply=True)

    assert result.action == "migrate"
    assert sorted(result.dropped) == ["todo_*", "todo_write"]
    assert not (agent / "00-tools.yaml").exists()
    spec = persona.load_prompt_spec(agent)
    assert spec.model == "omni/qwen-free-random"
    assert spec.reasoning_effort == "xhigh"
    assert spec.max_output_tokens == 8192
    assert spec.sampling == {"temperature": 0.2}
    assert spec.tool_selectors == ("read:eager", "write:eager", "wiki_*:eager", "reviewer:eager")
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
