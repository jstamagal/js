from __future__ import annotations

from js import paths
from js.toolkit import ToolContext, fs, plan


def test_plan_writes_markdown_under_plans_dir_and_reports_target(tmp_path):
    # Plans go to ~/.js/plans/<name>-<version>.md, never into the working directory.
    project = tmp_path / "project"
    project.mkdir()
    context = ToolContext(cwd=project)

    result = plan.plan(
        plan_name="rollout",
        version="v2",
        content="# Rollout\n\n- step one\n",
        context=context,
    )

    target = paths.plans_dir() / "rollout-v2.md"
    assert result == f"plan written to {target}"
    assert target.is_file()
    assert target.read_text() == "# Rollout\n\n- step one\n"
    # The parent dir was created on demand.
    assert paths.plans_dir().is_dir()
    assert not (project / "plans").exists()


def test_plan_sanitizes_unsafe_name_and_version_into_filename(tmp_path):
    # Non [alnum-_.] chars become "-", edges stripped.
    context = ToolContext(cwd=tmp_path)

    result = plan.plan(
        plan_name="my plan/draft",
        version="1.0 beta",
        content="body",
        context=context,
    )

    target = paths.plans_dir() / "my-plan-draft-1.0-beta.md"
    assert result == f"plan written to {target}"
    assert target.read_text() == "body"


def test_plan_empty_name_and_version_fall_back_to_defaults(tmp_path):
    # Fully-stripped names default to "plan"/"v1".
    context = ToolContext(cwd=tmp_path)

    result = plan.plan(plan_name="///", version="...", content="x", context=context)

    target = paths.plans_dir() / "plan-v1.md"
    assert result == f"plan written to {target}"
    assert target.is_file()


def test_plan_snapshot_lets_undo_restore_prior_plan(tmp_path):
    # snapshot() captures pre-write state for undo support.
    context = ToolContext(cwd=tmp_path)
    target = paths.plans_dir() / "rollout-v1.md"

    plan.plan(plan_name="rollout", version="v1", content="first", context=context)
    result = plan.plan(
        plan_name="rollout",
        version="v1",
        content="second",
        overwrite=True,
        context=context,
    )

    assert result == f"plan overwritten at {target}"
    assert target.read_text() == "second"
    # Two writes -> two undo entries; the first recorded a file that did not exist.
    assert context.undo.count(target) == 2
    assert fs.undo(str(target), context=context).startswith("restored")
    assert target.read_text() == "first"
    assert fs.undo(str(target), context=context).startswith("restored")
    assert not target.exists()


def test_plan_refuses_to_silently_replace_an_existing_version(tmp_path):
    context = ToolContext(cwd=tmp_path)
    target = paths.plans_dir() / "rollout-v1.md"
    plan.plan(plan_name="rollout", version="v1", content="first", context=context)

    result = plan.plan(plan_name="rollout", version="v1", content="second", context=context)

    assert result == f"ERROR: plan already exists at {target}; pass overwrite=true to replace it"
    assert target.read_text(encoding="utf-8") == "first"


def test_plan_rejects_a_name_over_the_filename_limit(tmp_path):
    import os

    context = ToolContext(cwd=tmp_path)

    result = plan.plan(plan_name="p" * 300, version="v1", content="x", context=context)

    assert result.startswith("ERROR:")
    assert str(os.pathconf(tmp_path, "PC_NAME_MAX")) in result
    assert not (paths.plans_dir()).exists()
