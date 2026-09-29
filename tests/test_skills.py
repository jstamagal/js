from pathlib import Path
import re

import pytest

from js.skills import (
    SkillCatalog,
    SkillInvocationError,
    ToolActivationResult,
    discover_skills,
    expand_user_invocation,
    load_skill,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _catalog(tmp_path: Path, package: Path, global_dir: Path) -> SkillCatalog:
    return discover_skills(
        tmp_path,
        builtin_dir=package,
        global_dir=global_dir,
        user_dir=tmp_path / "user-agents-skills",
    )


def test_skill_catalog_indexes_metadata_without_instruction_body(tmp_path):
    sentinel = "BODY_ONLY_SENTINEL_92a441"
    package = tmp_path / "package"
    global_dir = tmp_path / "global"
    _write(
        package / "deploy" / "SKILL.md",
        "---\nname: deploy\ndescription: Ship a release safely\ntools: [shell, read]\n---\n"
        f"# ignored\n\n{sentinel}\n",
    )

    catalog = _catalog(tmp_path / "project", package, global_dir)
    metadata = catalog.lookup("DEPLOY")

    assert metadata is not None
    assert metadata.name == "deploy"
    assert metadata.description == "Ship a release safely"
    assert metadata.tools == ("shell", "read")
    assert sentinel not in repr(catalog.skills)
    assert catalog.search(sentinel) == ()
    assert catalog.load("deploy") == f"# ignored\n\n{sentinel}\n"


def test_explicit_loader_activates_declared_tools_and_reports_failures_in_manifest_order(
    tmp_path,
):
    package = tmp_path / "package"
    sentinel = "FULL_INSTRUCTIONS_0d489a"
    _write(
        package / "deploy" / "SKILL.md",
        "---\nname: deploy\ndescription: Deploy safely\n"
        "tools: [shell, browser, missing, denied]\n---\n"
        f"# Deploy\n\n{sentinel}\n",
    )
    catalog = _catalog(tmp_path / "project", package, tmp_path / "global")

    class Activator:
        def __init__(self):
            self.requested = None

        def activate_tools(self, names):
            self.requested = names
            return ToolActivationResult(
                activated=("browser", "shell"),
                denied=("denied",),
                missing=("missing",),
            )

    activator = Activator()
    loaded = load_skill(catalog, "DEPLOY", tool_registry=activator)

    assert loaded is not None
    assert activator.requested == ("shell", "browser", "missing", "denied")
    assert loaded.instructions == f"# Deploy\n\n{sentinel}\n"
    assert loaded.activation.activated == ("shell", "browser")
    assert loaded.render() == (
        f"# Deploy\n\n{sentinel}\n\n"
        "Skill tool requirements unavailable: policy-denied: denied; unknown: missing"
    )


def test_explicit_loader_keeps_legacy_body_byte_for_byte_with_plain_registry(tmp_path):
    package = tmp_path / "package"
    body = "legacy body without trailing newline"
    _write(package / "legacy" / "SKILL.md", body)
    catalog = _catalog(tmp_path / "project", package, tmp_path / "global")

    loaded = catalog.load_exact("legacy", tool_registry=object())

    assert loaded is not None
    assert loaded.instructions == body
    assert loaded.render() == body


def test_skill_catalog_scans_every_layer_for_skill_dirs(tmp_path):
    package = tmp_path / "package"
    global_dir = tmp_path / "global"
    project = tmp_path / "project"
    user_dir = project / "user-agents-skills"
    expected = {
        "package-dir": _write(package / "package-dir" / "SKILL.md", "package dir"),
        "global-skill": _write(global_dir / "global-skill" / "SKILL.md", "global skill"),
        "user-skill": _write(user_dir / "user-skill" / "SKILL.md", "user skill"),
        "project-agents": _write(
            project / ".agents" / "skills" / "project-agents" / "SKILL.md",
            "project agents",
        ),
        "project-native": _write(
            project / ".js" / "skills" / "project-native" / "SKILL.md",
            "project native",
        ),
    }
    # Not skills: loose .md files and README-only dirs are ignored — the Agent
    # Skills format is a directory holding SKILL.md, nothing else counts.
    _write(project / ".agents" / "skills" / "loose-note.md", "loose note")
    _write(project / ".agents" / "skills" / "readme-only" / "README.md", "readme")

    catalog = _catalog(project, package, global_dir)

    assert {item.name: item.path for item in catalog.skills} == expected


def test_project_beats_global_beats_package_and_native_beats_agents_with_warning(
    tmp_path, capsys
):
    package = tmp_path / "package"
    global_dir = tmp_path / "global"
    project = tmp_path / "project"
    user_dir = project / "user-agents-skills"
    _write(package / "same" / "SKILL.md", "package")
    _write(global_dir / "same" / "SKILL.md", "global")
    _write(user_dir / "same" / "SKILL.md", "user")
    shadowed = _write(project / ".agents" / "skills" / "same" / "SKILL.md", "agents project")
    winning_path = _write(project / ".js" / "skills" / "same" / "SKILL.md", "project")

    catalog = _catalog(project, package, global_dir)

    assert catalog.lookup("same").source == "project"
    assert catalog.lookup("same").path == winning_path
    assert catalog.load("same") == "project"
    assert str(shadowed) in capsys.readouterr().err


def test_metadata_is_derived_and_bounded(tmp_path):
    package = tmp_path / "package"
    _write(package / "release-helper" / "SKILL.md", "# Release Helper\n\n" + "word " * 200)

    metadata = _catalog(tmp_path / "project", package, tmp_path / "global").lookup(
        "release-helper"
    )

    assert metadata is not None
    assert metadata.description.startswith("word word")
    assert len(metadata.description) == 500


def _listed_once(lines, text):
    return sum(text in line for line in lines) == 1


def test_catalog_skips_each_malformed_skill_and_warns_with_its_path(tmp_path, capsys):
    package = tmp_path / "package"
    project = tmp_path / "project"
    global_dir = tmp_path / "global"
    malformed = {
        "bad-yaml": "---\nname: [\n---\nbody",
        "bad-name": "---\nname: has space\n---\nbody",
        "no-close": "---\nname: never-closed\nbody",
        "duplicate-tools": "---\ntools: [ok, ok]\n---\nbody",
        "duplicate-second": "---\nname: duplicate\n---\ntwo",
    }
    _write(package / "valid" / "SKILL.md", "---\ndescription: Still indexed\n---\nvalid body")
    _write(package / "duplicate-first" / "SKILL.md", "---\nname: duplicate\n---\none")
    for dirname, text in malformed.items():
        _write(package / dirname / "SKILL.md", text)

    catalog = _catalog(project, package, global_dir)

    assert {skill.name for skill in catalog.skills} == {"duplicate", "valid"}
    warnings = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(warnings) == len(malformed)
    for dirname in malformed:
        assert _listed_once(warnings, str(package / dirname / "SKILL.md"))


def test_frontmatter_closing_delimiter_is_found_beyond_metadata_prefix(tmp_path):
    package = tmp_path / "package"
    filler = "# padding beyond the former prefix\n" * 3_000
    path = _write(
        package / "big" / "SKILL.md",
        "---\nname: big\ndescription: Large valid manifest\n" + filler + "---\nbody\n",
    )
    assert path.stat().st_size > 64 * 1024

    metadata = _catalog(tmp_path / "project", package, tmp_path / "global").lookup("big")

    assert metadata is not None
    assert metadata.description == "Large valid manifest"


def test_search_is_case_insensitive_term_based_and_deterministic(tmp_path):
    package = tmp_path / "package"
    _write(package / "zulu" / "SKILL.md", "---\ndescription: Release database safely\n---\nbody")
    _write(package / "Alpha" / "SKILL.md", "---\ndescription: Database release helper\n---\nbody")
    _write(
        package / "beta" / "SKILL.md",
        "---\ndescription: unrelated\ntools: [DatabaseTool]\n---\nbody",
    )
    catalog = _catalog(tmp_path / "project", package, tmp_path / "global")

    assert [item.name for item in catalog.search("DATABASE release")] == ["Alpha", "zulu"]
    assert [item.name for item in catalog.search("database")] == ["Alpha", "beta", "zulu"]
    assert catalog.lookup("aLpHa").name == "Alpha"


_USER_ONLY = "---\ndescription: Only when asked\ndisable-model-invocation: true\n---\nuser-only body\n"


def test_user_only_skill_is_hidden_from_model_and_unloadable_by_it(tmp_path):
    package = tmp_path / "package"
    _write(package / "secret" / "SKILL.md", _USER_ONLY)
    _write(package / "open" / "SKILL.md", "---\ndisable-model-invocation: false\n---\nopen body")

    catalog = _catalog(tmp_path / "project", package, tmp_path / "global")

    assert {skill.name for skill in catalog.skills} == {"open", "secret"}
    assert [skill.name for skill in catalog.model_skills] == ["open"]
    assert load_skill(catalog, "secret") is None
    assert catalog.load("secret") is None
    assert catalog.load("open") == "open body"
    assert catalog.load("secret", user=True) == "user-only body\n"


def test_non_boolean_disable_model_invocation_is_malformed(tmp_path, capsys):
    package = tmp_path / "package"
    bad = _write(package / "bad" / "SKILL.md", "---\ndisable-model-invocation: sometimes\n---\nx")

    catalog = _catalog(tmp_path / "project", package, tmp_path / "global")

    assert catalog.skills == ()
    assert str(bad) in capsys.readouterr().err


def test_user_invocation_loads_user_only_skill_with_request(tmp_path):
    package = tmp_path / "package"
    path = _write(package / "secret" / "SKILL.md", _USER_ONLY)
    catalog = _catalog(tmp_path / "project", package, tmp_path / "global")

    message = expand_user_invocation(catalog, "/skill SECRET sharpen this plan")

    assert message is not None
    assert "user-only body" in message
    assert message.rstrip().endswith("sharpen this plan")
    assert str(path.parent) in message
    assert "description: Only when asked" not in message


def test_user_invocation_ignores_other_lines_and_rejects_unknown_names(tmp_path):
    catalog = _catalog(tmp_path / "project", tmp_path / "package", tmp_path / "global")

    assert expand_user_invocation(catalog, "hello /skill x") is None
    assert expand_user_invocation(catalog, "/skills") is None
    with pytest.raises(SkillInvocationError):
        expand_user_invocation(catalog, "/skill nosuch")
    with pytest.raises(SkillInvocationError):
        expand_user_invocation(catalog, "/skill")


_BUILTIN_NINE = {
    "code-review",
    "codebase-design",
    "diagnosing-bugs",
    "grill-me",
    "grilling",
    "handoff",
    "improve-codebase-architecture",
    "wait-what",
    "wayfinder",
}


def test_fresh_install_lists_the_builtin_skills(tmp_path):
    catalog = discover_skills(tmp_path / "project")

    builtin = {skill.name for skill in catalog.skills if skill.source == "builtin"}
    assert builtin == _BUILTIN_NINE
    assert catalog.get("grill-me").model_invocable is False
    assert catalog.get("grilling").model_invocable is True
    # grill-me is an alias that sends the model to grilling.
    assert "grilling" in catalog.load("grill-me", user=True)


def test_user_skill_shadows_builtin_of_same_name(tmp_path):
    global_dir = tmp_path / "global"
    mine = _write(global_dir / "grilling" / "SKILL.md", "my grilling")

    catalog = discover_skills(tmp_path / "project", global_dir=global_dir)

    assert catalog.get("grilling").source == "global"
    assert catalog.get("grilling").path == mine
    assert catalog.load("grilling") == "my grilling"


def test_each_builtin_skill_names_its_upstream_source():
    from js.skills import BUILTIN_SKILLS_DIR

    for skill_md in BUILTIN_SKILLS_DIR.glob("*/SKILL.md"):
        sources = [
            line for line in skill_md.read_text().splitlines() if line.startswith("SOURCE: ")
        ]
        assert len(sources) == 1, skill_md
        assert re.fullmatch(
            r"SOURCE: https://github\.com/[\w.-]+/[\w.-]+/tree/[0-9a-f]{40}/\S+", sources[0]
        ), skill_md


def test_malformed_skill_warns_on_one_line_once(tmp_path, capsys):
    package = tmp_path / "package"
    bad = _write(package / "charts" / "SKILL.md", "---\nname: charts\ndescription: [unclosed\n---\nbody")

    for _ in range(3):
        catalog = _catalog(tmp_path / "project", package, tmp_path / "global")
        assert catalog.get("charts") is None

    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(lines) == 1
    assert str(bad) in lines[0]
