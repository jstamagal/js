from pathlib import Path

from js.skills import SkillCatalog, ToolActivationResult, discover_skills, load_skill


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _catalog(tmp_path: Path, package: Path, global_dir: Path) -> SkillCatalog:
    return discover_skills(tmp_path, package_dir=package, global_dir=global_dir)


def test_skill_catalog_indexes_metadata_without_instruction_body(tmp_path):
    sentinel = "BODY_ONLY_SENTINEL_92a441"
    package = tmp_path / "package"
    global_dir = tmp_path / "global"
    _write(
        package / "deploy.md",
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
    assert sentinel not in repr(catalog.search("release"))
    assert catalog.search(sentinel) == ()
    assert catalog.load("deploy") == f"# ignored\n\n{sentinel}\n"


def test_explicit_loader_activates_declared_tools_and_reports_failures_in_manifest_order(
    tmp_path,
):
    package = tmp_path / "package"
    sentinel = "FULL_INSTRUCTIONS_0d489a"
    _write(
        package / "deploy.md",
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
    _write(package / "legacy.md", body)
    catalog = _catalog(tmp_path / "project", package, tmp_path / "global")

    loaded = catalog.load_exact("legacy", tool_registry=object())

    assert loaded is not None
    assert loaded.instructions == body
    assert loaded.render() == body


def test_skill_catalog_supports_every_existing_layout(tmp_path):
    package = tmp_path / "package"
    global_dir = tmp_path / "global"
    project = tmp_path / "project"
    expected = {
        "package-flat": _write(package / "package-flat.md", "package flat"),
        "package-dir": _write(package / "package-dir" / "SKILL.md", "package dir"),
        "global-flat": _write(global_dir / "global-flat.md", "global flat"),
        "global-readme": _write(global_dir / "global-readme" / "README.md", "global readme"),
        "global-skill": _write(global_dir / "global-skill" / "SKILL.md", "global skill"),
        "project-flat": _write(project / "skills" / "project-flat.md", "project flat"),
        "project-readme": _write(
            project / "skills" / "project-readme" / "README.md", "project readme"
        ),
        "project-skill": _write(
            project / "skills" / "project-skill" / "SKILL.md", "project skill"
        ),
        "dot-flat": _write(project / ".skills" / "dot-flat.md", "dot flat"),
    }

    catalog = _catalog(project, package, global_dir)

    assert {item.name: item.path for item in catalog.skills} == expected


def test_project_overrides_global_over_package_and_skills_over_dotskills(tmp_path):
    package = tmp_path / "package"
    global_dir = tmp_path / "global"
    project = tmp_path / "project"
    _write(package / "same.md", "package")
    _write(global_dir / "same.md", "global")
    _write(project / ".skills" / "same.md", "dot project")
    winning_path = _write(project / "skills" / "same.md", "project")

    catalog = _catalog(project, package, global_dir)

    assert catalog.lookup("same").source == "project"
    assert catalog.lookup("same").path == winning_path
    assert catalog.load("same") == "project"


def test_metadata_is_derived_and_bounded(tmp_path):
    package = tmp_path / "package"
    _write(package / "release-helper.md", "# Release Helper\n\n" + "word " * 200)

    metadata = _catalog(tmp_path / "project", package, tmp_path / "global").lookup(
        "release-helper"
    )

    assert metadata is not None
    assert metadata.description.startswith("word word")
    assert len(metadata.description) == 500


def test_catalog_skips_each_malformed_skill_and_warns_with_its_path(tmp_path, capsys):
    package = tmp_path / "package"
    project = tmp_path / "project"
    global_dir = tmp_path / "global"
    malformed = {
        "bad-yaml.md": "---\nname: [\n---\nbody",
        "bad-name.md": "---\nname: has space\n---\nbody",
        "no-close.md": "---\nname: never-closed\nbody",
        "duplicate-tools.md": "---\ntools: [ok, ok]\n---\nbody",
        "duplicate-second.md": "---\nname: duplicate\n---\ntwo",
    }
    _write(package / "valid.md", "---\ndescription: Still indexed\n---\nvalid body")
    _write(package / "duplicate-first.md", "---\nname: duplicate\n---\none")
    for filename, text in malformed.items():
        _write(package / filename, text)

    catalog = _catalog(project, package, global_dir)

    assert {skill.name for skill in catalog.skills} == {"duplicate", "valid"}
    warnings = capsys.readouterr().err
    for filename in malformed:
        assert str(package / filename) in warnings
    assert warnings.count("WARNING: skipping malformed skill") == len(malformed)


def test_frontmatter_closing_delimiter_is_found_beyond_metadata_prefix(tmp_path):
    package = tmp_path / "package"
    filler = "# padding beyond the former prefix\n" * 3_000
    path = _write(
        package / "big.md",
        "---\nname: big\ndescription: Large valid manifest\n" + filler + "---\nbody\n",
    )
    assert path.stat().st_size > 64 * 1024

    metadata = _catalog(tmp_path / "project", package, tmp_path / "global").lookup("big")

    assert metadata is not None
    assert metadata.description == "Large valid manifest"


def test_search_is_case_insensitive_term_based_and_deterministic(tmp_path):
    package = tmp_path / "package"
    _write(package / "zulu.md", "---\ndescription: Release database safely\n---\nbody")
    _write(package / "Alpha.md", "---\ndescription: Database release helper\n---\nbody")
    _write(package / "beta.md", "---\ndescription: unrelated\ntools: [DatabaseTool]\n---\nbody")
    catalog = _catalog(tmp_path / "project", package, tmp_path / "global")

    assert [item.name for item in catalog.search("DATABASE release")] == ["Alpha", "zulu"]
    assert [item.name for item in catalog.search("database")] == ["Alpha", "beta", "zulu"]
    assert catalog.lookup("aLpHa").name == "Alpha"
