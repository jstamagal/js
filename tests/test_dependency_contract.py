from __future__ import annotations

from importlib import metadata, util
from pathlib import Path
import tomllib


def test_installed_provider_stack_matches_project_constraints():
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = set(project["project"]["dependencies"])

    expected = {
        "ai": "0.4.2",
        "openai": "2.44.0",
        "anthropic": "0.113.0",
        "httpx": "0.28.1",
    }
    for package, version in expected.items():
        assert f"{package}=={version}" in dependencies or any(
            dependency.startswith(f"{package}[") and dependency.endswith(f"=={version}")
            for dependency in dependencies
        )
        assert metadata.version(package) == version

    assert util.find_spec("httpx2") is None
    assert util.find_spec("httpcore2") is None
