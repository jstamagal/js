"""Skills the Agent Skills way: the catalog up front, the directory on load."""

from __future__ import annotations

from js import microcompact, skills
from js.toolkit import ToolContext, skill
from js.toolkit.registry import build_default_registry


def _write(root, name, text):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _catalog(tmp_path, project=None):
    return skills.discover_skills(
        project or tmp_path / "project", builtin_dir=tmp_path / "builtin",
        global_dir=tmp_path / "global", user_dir=tmp_path / "user")


def _skill_spec(surface):
    return next((spec["function"] for spec in surface.openai_specs()
                 if spec["function"]["name"] == "skill"), None)


def test_skill_tool_lists_every_loadable_skill_with_its_description(tmp_path):
    _write(tmp_path / "global", "deploy", "---\nname: deploy\ndescription: Ship it <safely>\n---\nbody\n")
    _write(tmp_path / "global", "secret",
           "---\nname: secret\ndescription: private\ndisable-model-invocation: true\n---\nbody\n")
    _write(tmp_path / "global", "off", "---\nname: off\ndescription: switched off\n---\nbody\n")
    catalog = skills.discover_skills(
        tmp_path / "project", builtin_dir=tmp_path / "builtin", global_dir=tmp_path / "global",
        user_dir=tmp_path / "user", disabled=["off"])
    surface = build_default_registry().select(["skill:eager"]).lazy_surface(
        tmp_path / "project", skill_catalog=catalog)

    spec = _skill_spec(surface)

    assert "<name>deploy</name>\n<description>Ship it &lt;safely&gt;</description>" in spec["description"]
    assert "secret" not in spec["description"] and "off" not in spec["parameters"]["properties"]["name"]["enum"]
    assert spec["parameters"]["properties"]["name"]["enum"] == ["deploy"]


def test_skill_tool_is_left_out_when_no_skill_is_loadable(tmp_path):
    surface = build_default_registry().select(["skill:eager"]).lazy_surface(
        tmp_path / "project", skill_catalog=_catalog(tmp_path))

    assert _skill_spec(surface) is None


def test_catalog_shows_the_nearest_layers_skill(tmp_path):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    _write(tmp_path / "global", "review", "---\nname: review\ndescription: global review\n---\nbody\n")
    _write(project / ".js" / "skills", "review", "---\nname: review\ndescription: project review\n---\nbody\n")
    surface = build_default_registry().select(["skill:eager"]).lazy_surface(
        project, skill_catalog=_catalog(tmp_path, project))

    description = _skill_spec(surface)["description"]

    assert "project review" in description
    assert "global review" not in description


def test_loaded_skill_names_its_directory_and_bundled_files(tmp_path):
    path = _write(tmp_path / "global", "pdf", "---\nname: pdf\ndescription: PDFs\n---\nRun scripts/extract.py.\n")
    (path.parent / "scripts").mkdir()
    (path.parent / "scripts" / "extract.py").write_text("")
    (path.parent / "references").mkdir()
    (path.parent / "references" / "spec.md").write_text("")
    context = ToolContext(cwd=tmp_path / "project")
    context.skill_catalog = _catalog(tmp_path)

    result = skill.skill("pdf", context=context)

    assert result.startswith('<skill_content name="pdf">\nRun scripts/extract.py.\n')
    assert f"Skill directory: {path.parent}\n" in result
    assert "<file>references/spec.md</file>\n<file>scripts/extract.py</file>" in result
    assert "<file>SKILL.md</file>" not in result


def test_user_invocation_carries_the_skill_directory(tmp_path):
    path = _write(tmp_path / "global", "notes", "---\nname: notes\ndescription: notes\n---\nTake notes.\n")

    message = skills.expand_user_invocation(_catalog(tmp_path), "/skill notes now")

    assert f"Skill directory: {path.parent}\n" in message
    assert message.endswith("</skill_content>\n\nnow")
    assert skills.user_invoked_skill(message) == "notes"


def test_off_spec_names_warn_and_still_load(tmp_path, capsys):
    skills.reset_warnings()
    _write(tmp_path / "global", "My_Skill", "---\ndescription: odd name\n---\nbody\n")
    _write(tmp_path / "global", "copied", "---\nname: original\ndescription: copied dir\n---\nbody\n")

    catalog = _catalog(tmp_path)

    assert {item.name for item in catalog.skills} == {"My_Skill", "original"}
    err = capsys.readouterr().err
    assert "My_Skill" in err
    assert "'copied'" in err


def test_microcompact_keeps_loaded_skill_content():
    body = '<skill_content name="deploy">\n' + "step\n" * 200 + "</skill_content>"
    messages = [
        {"role": "tool", "tool_call_id": "a", "name": "skill", "content": body},
        {"role": "tool", "tool_call_id": "b", "name": "read", "content": "x" * 5_000},
    ]

    microcompact.microcompact(messages, keep_recent=0)

    assert messages[0]["content"] == body
    assert messages[1]["content"] == microcompact.MICROCOMPACT_CLEARED_MESSAGE
