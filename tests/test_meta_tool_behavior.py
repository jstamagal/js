from __future__ import annotations

from js.toolkit import ToolContext
from js.toolkit import meta


def test_followup_returns_followup_required_stop_marker_with_bare_question():
    # js/toolkit/meta.py:48 — no options: just the marker line then the question.
    result = meta.followup("Which file should I edit?")

    assert result == "FOLLOWUP_REQUIRED\nWhich file should I edit?"
    assert result.splitlines()[0] == "FOLLOWUP_REQUIRED"


def test_followup_single_choice_numbers_only_truthy_options():
    # js/toolkit/meta.py:58-63 — falsy/None options are dropped, kept ones numbered.
    result = meta.followup(
        "Pick a color",
        option1="red",
        option2="",       # falsy: dropped
        option3="blue",
    )

    assert result == (
        "FOLLOWUP_REQUIRED\n"
        "Pick a color\n"
        "select one:\n"
        "1. red\n"
        "2. blue"
    )


def test_followup_multiple_flag_switches_choice_kind_line():
    # js/toolkit/meta.py:61 — multiple=True flips "select one" to "select one or more".
    result = meta.followup("Pick toppings", multiple=True, option1="cheese", option2="ham")

    assert "select one or more:" in result
    assert "select one:" not in result
    assert result.endswith("1. cheese\n2. ham")


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
    meta.plan(plan_name="rollout", version="v1", content="second", context=context)

    assert target.read_text() == "second"
    snaps = context.snapshots.get(target)
    assert snaps is not None
    # Two writes -> two snapshots; the first recorded a nonexistent file (None).
    assert snaps[0] is None
    assert snaps[1] == b"first"


def test_skill_loads_local_skill_markdown_from_skills_dir(tmp_path):
    # js/toolkit/meta.py:84 — local ./skills/<name>.md is a load candidate.
    context = ToolContext(cwd=tmp_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "deploy.md").write_text("# Deploy\n\nrun the thing\n")

    result = meta.skill("deploy", context=context)

    assert result == "# Deploy\n\nrun the thing\n"


def test_skill_loads_local_skill_from_named_dir_readme(tmp_path):
    # js/toolkit/meta.py:85 — ./skills/<name>/README.md is also a candidate.
    context = ToolContext(cwd=tmp_path)
    nested = tmp_path / "skills" / "review"
    nested.mkdir(parents=True)
    (nested / "README.md").write_text("review skill body")

    result = meta.skill("review", context=context)

    assert result == "review skill body"


def test_skill_loads_from_dotskills_dir(tmp_path):
    # js/toolkit/meta.py:86 — ./.skills/<name>.md is the last local candidate.
    context = ToolContext(cwd=tmp_path)
    dot = tmp_path / ".skills"
    dot.mkdir()
    (dot / "lint.md").write_text("lint skill")

    assert meta.skill("lint", context=context) == "lint skill"


def test_skill_errors_when_not_found_anywhere(tmp_path):
    # js/toolkit/meta.py:91 — no candidate matches -> explicit ERROR string.
    context = ToolContext(cwd=tmp_path)

    result = meta.skill("nope-no-such-skill-xyz", context=context)

    assert result == (
        "ERROR: skill 'nope-no-such-skill-xyz' not found in js/skills "
        "or local skills directories"
    )
