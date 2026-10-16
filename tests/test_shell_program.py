"""The shell tool runs `shell.program`, with pipefail where the shell has it,
and names the filtered environment only when the command asked for it."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from js import settings
from js.toolkit import ToolContext, process_net


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
needs_zsh = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")


def _exit_code(result: str) -> int:
    for line in result.splitlines():
        if line.startswith("exit="):
            return int(line.split("=", 1)[1])
    raise AssertionError(f"no exit line in {result!r}")


def _shell_line(result: str) -> str:
    return result.splitlines()[0]


def test_shell_program_defaults_to_bash():
    assert settings.default_value("shell.program") == "bash"
    assert ToolContext().shell_program == "bash"


@needs_bash
def test_default_context_runs_bash(tmp_path):
    result = process_net.shell('printf %s "${BASH_VERSION:+is-bash}"', context=ToolContext(cwd=tmp_path))

    assert Path(_shell_line(result).removeprefix("shell=")).name == "bash"
    assert "is-bash" in result


@needs_zsh
def test_shell_program_zsh_runs_zsh(tmp_path):
    context = ToolContext(cwd=tmp_path, shell_program="zsh")

    result = process_net.shell('printf %s "${ZSH_VERSION:+is-zsh}"', context=context)

    assert Path(_shell_line(result).removeprefix("shell=")).name == "zsh"
    assert "is-zsh" in result


@needs_bash
def test_bash_pipeline_reports_the_failing_stage(tmp_path):
    result = process_net.shell("false | cat", context=ToolContext(cwd=tmp_path, shell_program="bash"))

    assert _exit_code(result) == 1


@needs_zsh
def test_zsh_pipeline_reports_the_failing_stage(tmp_path):
    result = process_net.shell("false | cat", context=ToolContext(cwd=tmp_path, shell_program="zsh"))

    assert _exit_code(result) == 1


def test_sh_runs_without_pipefail(tmp_path):
    result = process_net.shell("false | cat", context=ToolContext(cwd=tmp_path, shell_program="/bin/sh"))

    assert _exit_code(result) == 0


def test_missing_shell_program_is_an_error(tmp_path):
    context = ToolContext(cwd=tmp_path, shell_program="no-such-shell-js-test")

    result = process_net.shell("true", context=context)

    assert result.startswith("ERROR:")
    assert "no-such-shell-js-test" in result


def test_failed_command_without_filtered_reference_has_no_env_preamble(tmp_path, monkeypatch):
    monkeypatch.setenv("JS_TEST_FILTERED", "secret")

    result = process_net.shell("echo nope >&2; exit 3", context=ToolContext(cwd=tmp_path, shell_program="/bin/sh"))

    assert _exit_code(result) == 3
    assert "environment=filtered" not in result


@pytest.mark.parametrize("reference", ["$JS_TEST_FILTERED", "${JS_TEST_FILTERED}", "${JS_TEST_FILTERED:-x}"])
def test_command_referencing_filtered_variable_gets_env_preamble(tmp_path, monkeypatch, reference):
    monkeypatch.setenv("JS_TEST_FILTERED", "secret")

    result = process_net.shell(f'echo "{reference}"', context=ToolContext(cwd=tmp_path, shell_program="/bin/sh"))

    assert "environment=filtered" in result
    assert "JS_TEST_FILTERED" in result


def test_allowed_or_unset_references_get_no_env_preamble(tmp_path, monkeypatch):
    monkeypatch.setenv("JS_TEST_PASSED", "fine")
    monkeypatch.delenv("JS_TEST_NEVER_SET", raising=False)
    context = ToolContext(cwd=tmp_path, shell_program="/bin/sh")

    result = process_net.shell(
        'echo "$JS_TEST_PASSED $JS_TEST_NEVER_SET $HOME"; exit 1',
        env=["JS_TEST_PASSED"],
        context=context,
    )

    assert "environment=filtered" not in result


def test_shell_own_variables_are_not_reported_as_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("SHLVL", "3")

    result = process_net.shell('echo "$SHLVL $PWD"; exit 1', context=ToolContext(cwd=tmp_path, shell_program="/bin/sh"))

    assert "environment=filtered" not in result


@needs_zsh
def test_zsh_passes_an_unmatched_glob_through_like_bash(tmp_path):
    context = ToolContext(cwd=tmp_path, shell_program="zsh")

    result = process_net.shell("printf %s --include=*.log", context=context)

    assert _exit_code(result) == 0
    assert "--include=*.log" in result
