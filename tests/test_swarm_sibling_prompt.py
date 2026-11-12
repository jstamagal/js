from __future__ import annotations

import pytest

from js import cli, swarm


@pytest.mark.parametrize("prompt", ["-v is broken, fix", "--fix this", "-4", "-4.5", "-.5", "plain opener", "-"])
@pytest.mark.parametrize("flag", ["-p", "--prompt"])
def test_sibling_consumes_actual_cli_prompt_values(monkeypatch, prompt, flag):
    parsed = []
    monkeypatch.setattr(cli, "_run_args", lambda args, _argv: parsed.append(args) or 0)
    parent = ["js", flag, prompt, "--swarm", "/fixture/parent", "--session", "test", "--model", "fixture-model"]
    assert cli.main(parent[1:]) == 0
    assert parsed[-1].prompt == prompt

    child = swarm.sibling_argv(parent, "/fixture/child", "child")

    assert cli.main(child[3:]) == 0
    assert parsed[-1].prompt == "-"
    assert parsed[-1].swarm == "/fixture/child"
    assert parsed[-1].session == "test-child"
    assert parsed[-1].model == "fixture-model"
    assert sum(arg in {"-p", "--prompt"} or arg.startswith("--prompt=") for arg in child[3:]) == 1
    if prompt != "-":
        assert prompt not in child


@pytest.mark.parametrize("prompt", ["-flag", "-v is broken, fix", "plain opener", ""])
def test_equals_prompt_is_replaced_once_without_parent_opener(monkeypatch, prompt):
    parsed = []
    monkeypatch.setattr(cli, "_run_args", lambda args, _argv: parsed.append(args) or 0)
    parent = ["js", "--prompt=" + prompt, "--swarm=/fixture/parent"]
    assert cli.main(parent[1:]) == 0
    assert parsed[-1].prompt == prompt

    child = swarm.sibling_argv(parent, "/fixture/child", "child")

    assert cli.main(child[3:]) == 0
    assert parsed[-1].prompt == "-"
    assert sum(arg in {"-p", "--prompt"} or arg.startswith("--prompt=") for arg in child[3:]) == 1
    assert "--prompt=" + prompt not in child


@pytest.mark.parametrize("prefix", [["-p"], ["--prompt"], []])
def test_missing_optional_prompt_does_not_consume_next_option(monkeypatch, prefix):
    parsed = []
    monkeypatch.setattr(cli, "_run_args", lambda args, _argv: parsed.append(args) or 0)
    parent = ["js", *prefix, "--model", "fixture-model", "--swarm", "/fixture/parent"]
    child = swarm.sibling_argv(parent, "/fixture/child", "child")
    assert cli.main(child[3:]) == 0
    assert parsed[-1].prompt == "-"
    assert parsed[-1].model == "fixture-model"
    assert sum(arg in {"-p", "--prompt"} or arg.startswith("--prompt=") for arg in child[3:]) == 1
