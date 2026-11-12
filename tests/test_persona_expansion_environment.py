"""Derived agent expansion variables belong to its child environment only."""

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from js import persona, promptexpand, secret_patterns


@pytest.mark.parametrize("existing", [False, True])
def test_agent_expansion_keeps_ambient_environment_unchanged(monkeypatch, existing):
    names = ("JS_ROOT", "JS_SHELL", secret_patterns.ENV_VAR)
    for name in names:
        if existing:
            monkeypatch.setenv(name, "ambient-" + name)
        else:
            monkeypatch.delenv(name, raising=False)
    before = dict(os.environ)
    cfg = SimpleNamespace(allow_inline_code=True, settings={"SHELL_PROGRAM": "/bin/sh"})
    expected = str(Path(persona.__file__).resolve().parents[1]) + "|/bin/sh"
    result = persona.expand_agent_text(
        "{{JS_ROOT}}|!{env JS_SHELL}\n!{sh printf '%s|%s' \"$JS_ROOT\" \"$JS_SHELL\"}", cfg,
    )
    assert result == expected + "\n" + expected
    assert dict(os.environ) == before


def test_lookup_override_does_not_change_ordinary_code_environment(monkeypatch):
    monkeypatch.setenv("EXPANSION_SENTINEL", "ambient")
    assert promptexpand.expand_prompt(
        "{{EXPANSION_SENTINEL}}|!{sh printf '%s' \"$EXPANSION_SENTINEL\"}",
        env={"EXPANSION_SENTINEL": "lookup"}, allow_code=True,
    ) == "lookup|ambient"


def test_explicit_code_environment_is_isolated_between_concurrent_expansions():
    barrier = threading.Barrier(2)
    def runner(body, *, timeout_s, max_output_bytes):
        barrier.wait(timeout=2)
        return promptexpand._run_capture(
            [sys.executable, "-c", "import os; print(os.environ['EXPANSION_SENTINEL'], end='')"],
            timeout_s=timeout_s, max_output_bytes=max_output_bytes, label="python",
        )
    def expand(value):
        return promptexpand.expand_prompt(
            "!{custom run}", allow_code=True, code_env={"EXPANSION_SENTINEL": value},
            subsystems={"custom": (True, runner)},
        )
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(expand, ["one", "two"])) == ["one", "two"]


def test_explicit_code_environment_resets_after_expansion_error(monkeypatch):
    monkeypatch.setenv("EXPANSION_SENTINEL", "ambient")
    with pytest.raises(promptexpand.PromptExpansionError):
        promptexpand.expand_prompt("!{unknown body}", code_env={"EXPANSION_SENTINEL": "private"}, on_error="raise")
    assert promptexpand.expand_prompt(
        "!{sh printf '%s' \"$EXPANSION_SENTINEL\"}", allow_code=True,
    ) == "ambient"
