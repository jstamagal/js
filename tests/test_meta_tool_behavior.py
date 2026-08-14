from __future__ import annotations

from pathlib import Path

from js.skills import ToolActivationResult
from js.toolkit import ToolContext
from js.toolkit import meta
from js.toolkit.registry import select


def test_plan_writes_markdown_under_plans_dir_and_reports_target(tmp_path):
    # js/toolkit/meta.py:67-75 — writes ./plans/<name>-<version>.md relative to cwd.
    context = ToolContext(cwd=tmp_path)

    result = meta.plan(
        plan_name="rollout",
        version="v2",
        content="# Rollout\n\n- step one\n",
        context=context,
    )

    target = tmp_path / "plans" / "rollout-v2.md"
    assert result == f"plan written to {target}"
    assert target.is_file()
    assert target.read_text() == "# Rollout\n\n- step one\n"
    # The parent dir was created on demand.
    assert (tmp_path / "plans").is_dir()


def test_plan_sanitizes_unsafe_name_and_version_into_filename(tmp_path):
    # js/toolkit/meta.py:69-70 — non [alnum-_.] chars become "-", edges stripped.
    context = ToolContext(cwd=tmp_path)

    result = meta.plan(
        plan_name="my plan/draft",
        version="1.0 beta",
        content="body",
        context=context,
    )

    target = tmp_path / "plans" / "my-plan-draft-1.0-beta.md"
    assert result == f"plan written to {target}"
    assert target.read_text() == "body"


def test_plan_empty_name_and_version_fall_back_to_defaults(tmp_path):
    # js/toolkit/meta.py:69-70 — fully-stripped names default to "plan"/"v1".
    context = ToolContext(cwd=tmp_path)

    result = meta.plan(plan_name="///", version="...", content="x", context=context)

    target = tmp_path / "plans" / "plan-v1.md"
    assert result == f"plan written to {target}"
    assert target.is_file()


def test_plan_snapshot_lets_undo_restore_prior_plan(tmp_path):
    # js/toolkit/meta.py:72 — snapshot() captures pre-write state for undo support.
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "plans" / "rollout-v1.md"

    meta.plan(plan_name="rollout", version="v1", content="first", context=context)
    result = meta.plan(
        plan_name="rollout",
        version="v1",
        content="second",
        overwrite=True,
        context=context,
    )

    assert result == f"plan overwritten at {target}"
    assert target.read_text() == "second"
    snaps = context.snapshots.get(target)
    assert snaps is not None
    # Two writes -> two snapshots; the first recorded a nonexistent file (None).
    assert snaps[0] is None
    assert snaps[1] == b"first"


def test_plan_refuses_to_silently_replace_an_existing_version(tmp_path):
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "plans" / "rollout-v1.md"
    meta.plan(plan_name="rollout", version="v1", content="first", context=context)

    result = meta.plan(plan_name="rollout", version="v1", content="second", context=context)

    assert result == f"ERROR: plan already exists at {target}; pass overwrite=true to replace it"
    assert target.read_text(encoding="utf-8") == "first"


def _write_skill(root: Path, name: str, text: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_skill_loads_local_skill_from_agents_skills_dir(tmp_path):
    # Project skills live in ./.agents/skills/<name>/SKILL.md.
    context = ToolContext(cwd=tmp_path)
    _write_skill(tmp_path / ".agents" / "skills", "deploy", "# Deploy\n\nrun the thing\n")

    result = meta.skill("deploy", context=context)

    assert result == "# Deploy\n\nrun the thing\n"


def test_skill_loads_from_native_js_skills_dir(tmp_path):
    # ./.js/skills/<name>/SKILL.md is the client-native project location.
    context = ToolContext(cwd=tmp_path)
    _write_skill(tmp_path / ".js" / "skills", "lint", "lint skill")

    assert meta.skill("lint", context=context) == "lint skill"


def test_skill_with_declared_tools_is_unchanged_for_plain_registry(tmp_path):
    context = ToolContext(cwd=tmp_path)
    context.tool_registry = select(["shell"])
    _write_skill(
        tmp_path / ".agents" / "skills",
        "legacy",
        "---\ntools: [shell, missing]\n---\nlegacy instructions",
    )

    assert meta.skill("legacy", context=context) == "legacy instructions"


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

    result = meta.skill("deploy", context=context)

    assert context.tool_registry.requested == ("shell", "browser", "absent")
    assert result == (
        "# Deploy\n\nrun it\n\n"
        "Skill tool requirements unavailable: policy-denied: browser; unknown: absent"
    )


def test_skill_errors_when_not_found_anywhere(tmp_path):
    # js/toolkit/meta.py:91 — no candidate matches -> explicit ERROR string.
    context = ToolContext(cwd=tmp_path)

    result = meta.skill("nope-no-such-skill-xyz", context=context)

    assert result == (
        "ERROR: skill 'nope-no-such-skill-xyz' not found in js/skills "
        "or local skills directories"
    )
