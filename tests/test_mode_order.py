"""Which mode a command line runs when it names two, and what each refusal
exits with. Every mode's endpoint is a recorder, so a case pins the first
thing the run did and its exit code."""

from __future__ import annotations

import pytest

from js import (
    agent_lock,
    bench,
    cli,
    commit_cli,
    listings,
    login_cli,
    messages as msgs,
    migrate_config,
    model_metadata,
    paths,
    prompt_run,
    verify,
)


@pytest.fixture
def ran(monkeypatch, tmp_path):
    """The modes and refusals a run reached, in order."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_warn_missing_binaries", lambda: None)
    calls: list[object] = []

    def record(name, code=0):
        return lambda *args, **kwargs: calls.append(name) or code

    monkeypatch.setattr(verify, "verify", lambda agent: calls.append("verify") or [])
    monkeypatch.setattr(listings, "print_session_list", record("list-sessions"))
    monkeypatch.setattr(login_cli, "main", lambda argv: calls.append(("login", *argv)) or 0)
    monkeypatch.setattr(paths, "agent_names", lambda: calls.append("list-agents") or [])
    monkeypatch.setattr(listings, "providers_json", lambda: calls.append("list-providers") or [])
    monkeypatch.setattr(listings, "logins_json", lambda: calls.append("list-logins") or [])
    monkeypatch.setattr(listings, "print_model_list", record("list-models"))
    monkeypatch.setattr(listings, "models_json", lambda *args: calls.append("list-models") or {})
    monkeypatch.setattr(model_metadata, "force_refresh_model_catalog", lambda: calls.append("refresh") or True)
    monkeypatch.setattr(bench, "run_bench", record("bench"))
    monkeypatch.setattr(cli, "_run_compact_offline", record("compact"))
    monkeypatch.setattr(migrate_config, "run_migrate_config", record("migrate-config"))
    monkeypatch.setattr(commit_cli, "run_commit", record("commit"))
    monkeypatch.setattr(prompt_run, "run", record("prompt"))
    monkeypatch.setattr(msgs, "warn", lambda message, **fields: calls.append(message))
    return calls


@pytest.mark.parametrize("argv, first, code", [
    (["--verify", "--list-sessions", "--json"], "verify", 0),
    (["--list-sessions", "--json", "--last"], "list-sessions", 0),
    (["--list-sessions", "--json", "-s", "named"], msgs.LIST_EXCLUSIVE, 2),
    (["--last", "-s", "named"], msgs.LAST_WITH_SESSION, 2),
    (["--last", "--list-agents"], msgs.NO_PREVIOUS_SESSION, 2),
    (["--list-agents", "--login", "deepseek"], ("login", "deepseek"), 0),
    (["--list-agents", "--login"], ("login",), 0),
    (["--list-agents", "--logout", "deepseek"], ("login", "logout", "deepseek"), 0),
    (["--list-agents", "--list-providers", "--json"], "list-agents", 0),
    (["--list-providers", "--list-logins", "--json"], "list-providers", 0),
    (["--list-logins", "--list-models", "--json"], "list-logins", 0),
    (["--list-models", "openai", "--refresh-model-catalog"], "list-models", 0),
    (["--refresh-model-catalog", "--debug", "--debug-file", "trace"], "refresh", 0),
    (["--list-agents", "--debug", "--debug-file", "trace"], "list-agents", 0),
    (["--debug", "--debug-file", "trace", "--commit", "--compact", "s"], msgs.DEBUG_FLAGS_EXCLUSIVE, 2),
    (["--commit", "--compact", "s"], msgs.MODES_EXCLUSIVE, 2),
    (["--compact", "s", "-f", "x"], msgs.FILE_NEEDS_PROMPT_MODE, 2),
    (["--bench", "b", "--compact", "s"], msgs.BENCH_EXCLUSIVE, 2),
    (["--bench", "b", "--agent", "other"], msgs.BENCH_EXCLUSIVE, 2),
    (["--bench", "b", "-p", "hi"], "bench", 0),
    (["--compact", "s", "--migrate-config"], "compact", 0),
    (["--migrate-config", "--commit"], "migrate-config", 0),
    (["--commit", "--agent", "other"], msgs.COMMIT_WITH_AGENT, 2),
    (["--commit", "-p", "hi"], "commit", 0),
    (["-f", "x"], msgs.FILE_NEEDS_PROMPT, 2),
    (["-p", "hi"], "prompt", 0),
])
def test_the_first_mode_on_the_command_line_wins(ran, argv, first, code):
    assert cli.main(argv) == code
    assert ran[0] == first


def test_refresh_model_catalog_runs_the_mode_after_it(ran):
    assert cli.main(["--refresh-model-catalog", "--migrate-config"]) == 0
    assert ran == ["refresh", "migrate-config"]


def test_a_failed_catalog_refresh_exits_one_before_the_next_mode(ran, monkeypatch):
    monkeypatch.setattr(model_metadata, "force_refresh_model_catalog", lambda: ran.append("refresh") or False)
    assert cli.main(["--refresh-model-catalog", "--migrate-config"]) == 1
    assert ran == ["refresh"]


def test_lock_is_taken_after_the_listings_and_before_a_run(ran, monkeypatch):
    def busy(agent):
        raise agent_lock.Busy(agent)

    monkeypatch.setattr(agent_lock, "acquire", busy)
    assert cli.main(["--lock", "--list-agents"]) == 0
    assert ran == ["list-agents"]
    assert cli.main(["--lock", "-p", "hi"]) == 2
    assert ran[1:] == [msgs.FAILED]


def test_json_with_nothing_to_run_headless_exits_two(ran):
    assert cli.main(["--json"]) == 2
    assert "prompt" not in ran
