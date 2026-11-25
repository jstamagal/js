"""Prompt command names and argument spans share slash-whitespace handling."""

from pathlib import Path

import pytest

from js.prompt_commands import PromptCommand, expand


@pytest.mark.parametrize("prefix", ["/review", "/ review", "/\treview", "  /  review"])
def test_whitespace_after_slash_preserves_argument_tokens(prefix):
    command = PromptCommand("review", Path("review.md"), "", "ARGS=[$ARGUMENTS] ONE=[$1]")
    assert expand(prefix + " foo bar", {"review": command}) == "ARGS=[foo bar] ONE=[foo]"


@pytest.mark.parametrize("prefix", ["/review", "/ review", "/\treview"])
def test_argument_first_line_indentation_survives_name_whitespace(prefix):
    command = PromptCommand("review", Path("review.md"), "", "Code:\n$ARGUMENTS")
    assert expand(prefix + "     def f():\n    return 1", {"review": command}) == (
        "Code:\n    def f():\n    return 1"
    )


def test_whitespace_only_or_unknown_command_is_not_expanded():
    command = PromptCommand("review", Path("review.md"), "", "ARGS=[$ARGUMENTS]")
    commands = {"review": command}
    assert expand("/   ", commands) is None
    assert expand("/ unknown foo", commands) is None
    assert expand("/ review", commands) == "ARGS=[]"
