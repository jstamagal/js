"""Edited skill frontmatter fails cleanly before any tools are activated."""

import json

import pytest

from js import skills
from js.toolkit import ToolContext, meta
from js.toolkit.registry import build_default_registry


VALID = "---\ndescription: inspect files\ntools: [read]\n---\nRead carefully.\n"


@pytest.mark.parametrize("broken", ["---\ndescription: missing end\n", "---\nname: [broken\n---\nbody\n", None])
@pytest.mark.parametrize("rail", ["discovery", "native", "user"])
def test_changed_skill_load_is_clean_and_does_not_activate(tmp_path, broken, rail):
    root = tmp_path / "skills"
    path = root / "inspect" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(VALID)
    catalog = skills.discover_skills(tmp_path, builtin_dir=tmp_path / "none",
                                     global_dir=root, user_dir=tmp_path / "user")
    surface = build_default_registry().select(["skill:eager", "read:lazy"]).lazy_surface(
        tmp_path, skill_catalog=catalog)
    context = ToolContext(cwd=tmp_path)
    context.skill_catalog = catalog
    context.tool_registry = surface
    changes = []
    surface.on_change = changes.append
    before = surface.snapshot()
    if broken is None:
        path.unlink()
    else:
        path.write_text(broken)

    if rail == "user":
        with pytest.raises(skills.SkillInvocationError):
            skills.expand_user_invocation(catalog, "/skill inspect")
    elif rail == "native":
        assert meta.skill("inspect", context).startswith("ERROR")
    else:
        assert surface.discover(load="skill:inspect").startswith("ERROR")

    assert surface.snapshot() == before
    assert not changes
    assert surface.resolve("read") is None
    path.write_text(VALID)
    if rail == "user":
        assert "Read carefully." in skills.expand_user_invocation(catalog, "/skill inspect")
    elif rail == "native":
        assert "Read carefully." in meta.skill("inspect", context)
        assert surface.resolve("read") is not None
    else:
        assert "Read carefully." in json.loads(surface.discover(load="skill:inspect"))["instructions"]
        assert surface.resolve("read") is not None
