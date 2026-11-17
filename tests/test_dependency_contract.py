from __future__ import annotations

import re
import tomllib
from importlib import metadata
from pathlib import Path


def _release(version: str) -> tuple[int, ...]:
    """The numeric release part of a version, compared as numbers."""
    return tuple(int(part) for part in re.findall(r"\d+", version.split("+")[0])[:3])


def test_installed_provider_stack_matches_project_constraints():
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = set(project["project"]["dependencies"])

    expected = {
        "ai": "0.9.0",
        "openai": "3.52.0",
        "anthropic": "1.17.0",
        "httpx": "0.28.1",
    }
    for package, version in expected.items():
        assert any(
            dependency == f"{package}>={version}"
            or dependency.startswith(f"{package}[")
            and dependency.endswith(f">={version}")
            for dependency in dependencies
        )
        assert _release(metadata.version(package)) >= _release(version)
