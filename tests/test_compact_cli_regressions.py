"""Offline compaction CLI routing and notice regressions."""

import io

import pytest

from js import cli


@pytest.mark.parametrize("arguments, stdin, expected", [
    (["-p"], "keep the API notes\n", "keep the API notes\n"),
    (["-p", "-"], "focus from pipe", "focus from pipe"),
    (["-p", "explicit focus"], "ignored pipe", "explicit focus"),
    ([], "ignored pipe", ""),
])
def test_compact_cli_reads_stdin_focus_only_when_requested(monkeypatch, arguments, stdin, expected):
    calls = []

    def compact(session, **kwargs):
        calls.append((session, kwargs))
        return 0

    monkeypatch.setattr(cli, "_run_compact_offline", compact)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(stdin))
    assert cli.main(["--compact", "test-session", *arguments]) == 0
    assert len(calls) == 1
    assert calls[0][0] == "test-session"
    assert calls[0][1]["focus"] == expected
