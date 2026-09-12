from __future__ import annotations

from importlib import metadata
from pathlib import Path
import tomllib


def test_installed_provider_stack_matches_project_constraints():
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = set(project["project"]["dependencies"])

    expected = {
        "ai": "0.5.2",
        "openai": "3.13.0",
        "anthropic": "1.5.0",
        "httpx": "0.28.1",
    }
    for package, version in expected.items():
        assert any(
            dependency == f"{package}>={version}"
            or dependency.startswith(f"{package}[")
            and dependency.endswith(f">={version}")
            for dependency in dependencies
        )
        assert metadata.version(package) >= version
