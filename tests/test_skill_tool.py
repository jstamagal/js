from __future__ import annotations

from pathlib import Path

from js.skills import ToolActivationResult
from js.toolkit import ToolContext, skill
from js.toolkit.registry import select


def _write_skill(root: Path, name: str, text: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_skill_loads_local_skill_from_agents_skills_dir(tmp_path):
    # Project skills live in ./.agents/skills/<name>/SKILL.md.
    context = ToolContext(cwd=tmp_path)
    _write_skill(tmp_path / ".agents" / "skills", "deploy", "# Deploy\n\nrun the thing\n")

    result = skill.skill("deploy", context=context)

    assert result == "# Deploy\n\nrun the thing\n"


def test_skill_loads_from_native_js_skills_dir(tmp_path):
    # ./.js/skills/<name>/SKILL.md is the client-native project location.
    context = ToolContext(cwd=tmp_path)
    _write_skill(tmp_path / ".js" / "skills", "lint", "lint skill")

    assert skill.skill("lint", context=context) == "lint skill"


def test_skill_tool_refuses_user_only_skill(tmp_path):
    context = ToolContext(cwd=tmp_path)
    _write_skill(
        tmp_path / ".js" / "skills",
        "secret",
        "---\ndisable-model-invocation: true\n---\nuser-only body\n",
    )

    result = skill.skill("secret", context=context)

    assert result.startswith("ERROR:")
    assert "user-only body" not in result


def test_skill_with_declared_tools_is_unchanged_for_plain_registry(tmp_path):
    context = ToolContext(cwd=tmp_path)
    context.tool_registry = select(["shell:lazy"])
    _write_skill(
        tmp_path / ".agents" / "skills",
        "legacy",
        "---\ntools: [shell, missing]\n---\nlegacy instructions",
    )

    assert skill.skill("legacy", context=context) == "legacy instructions"


def test_skill_invocation_activates_declared_tools_and_still_returns_instructions(tmp_path):
    context = ToolContext(cwd=tmp_path)
    _write_skill(
        tmp_path / ".agents" / "skills",
        "deploy",
        "---\ntools: [shell, browser, absent]\n---\n# Deploy\n\nrun it\n",
    )

    class Activator:
        def __init__(self):
            self.requested = None

        def activate_tools(self, names):
            self.requested = names
            return ToolActivationResult(
                activated=("shell",), denied=("browser",), missing=("absent",)
            )

    context.tool_registry = Activator()

    result = skill.skill("deploy", context=context)

    assert context.tool_registry.requested == ("shell", "browser", "absent")
    assert result == (
        "# Deploy\n\nrun it\n\n"
        "Skill tool requirements unavailable: policy-denied: browser; unknown: absent"
    )


def test_skill_errors_when_not_found_anywhere(tmp_path):
    # No candidate matches -> explicit ERROR string.
    context = ToolContext(cwd=tmp_path)

    result = skill.skill("nope-no-such-skill-xyz", context=context)

    assert result == (
        "ERROR: skill 'nope-no-such-skill-xyz' not found in js/skills "
        "or local skills directories"
    )
