"""Skill frontmatter normalization keeps match-all paths unscoped."""

import pytest

from js.skills import _paths_field


@pytest.mark.parametrize("patterns", [
    "**/*", "**", "/**/*/", "/**/",
    ["src/**", "**/*"], ["**", "src/**"],
    "src/**, **/*", "{src/**,**/*}", [],
])
def test_universal_path_is_unscoped(patterns):
    assert _paths_field({"paths": patterns}) == ()


@pytest.mark.parametrize("patterns, expected", [
    ("src/**", ("src/**",)),
    (["*.py", "docs/**"], ("*.py", "docs/**")),
    ("src/*.{ts,tsx}", ("src/*.ts", "src/*.tsx")),
])
def test_restrictive_paths_keep_their_scope(patterns, expected):
    assert _paths_field({"paths": patterns}) == expected
