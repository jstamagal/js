"""Skill frontmatter normalization keeps match-all paths unscoped."""

import pytest

from js.skills import _paths_field, _tools_field


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


@pytest.mark.parametrize("tools", ["read, shell,", ", read,, shell, ", "read,shell", "read, ,shell"])
def test_string_tools_ignore_empty_comma_parts(tools):
    assert _tools_field({"tools": tools}) == ("read", "shell")


@pytest.mark.parametrize("tools", [
    ["read", ""], ["read", " "], ["read", None],
    "read,../shell,", "read, shell;echo,", "read, read,",
    ["read", "read"],
])
def test_tools_still_reject_unsafe_or_duplicate_entries(tools):
    with pytest.raises(ValueError):
        _tools_field({"tools": tools})
