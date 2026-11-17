"""Offline compaction CLI routing and notice regressions."""

import io
from types import SimpleNamespace

import pytest
from test_config_compaction_layers import _compact_test_cfg

from js import cli, compaction_settings, paths, repl_session_commands, repl_state, run_assembly
from js.repl_state import ReplState


@pytest.mark.parametrize("arguments, stdin, expected", [
    (["-p"], "keep the API notes\n", "keep the API notes\n"),
    (["-p", "-"], "focus from pipe", "focus from pipe"),
    (["-p", "explicit focus"], "ignored pipe", "explicit focus"),
    ([], "ignored pipe", ""),
])
def test_compact_cli_reads_stdin_focus_only_when_requested(monkeypatch, tmp_path, arguments, stdin, expected):
    cfg = _compact_test_cfg(tmp_path, {})
    cfg.session_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.session_file.touch()
    sessions, calls = [], []

    def from_env(session, *args, **kwargs):
        sessions.append(session)
        return cfg

    def compact(*args, **kwargs):
        calls.append(kwargs)
        return "summary complete"

    monkeypatch.setattr(run_assembly, "from_env", from_env)
    monkeypatch.setattr(cli.P, "load_configured_prompt_spec", lambda cfg: SimpleNamespace(system=""))
    monkeypatch.setattr(cli.compaction, "compact_now_sync", compact)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(stdin))
    assert cli.main(["--compact", "test-session", *arguments]) == 0
    assert len(calls) == 1
    assert sessions == ["test-session"]
    assert calls[0]["focus"] == expected


@pytest.mark.parametrize("route", ["manual", "offline"])
@pytest.mark.parametrize("stage, detail, done_expected", [
    ("skipped", "no prefix available", False),
    ("skipped", "different diagnostic", True),
    ("committed", "no prefix available", True),
    (None, None, True),
])
def test_compact_only_deduplicates_already_published_skip(monkeypatch, tmp_path, route, stage, detail, done_expected):
    cfg = _compact_test_cfg(tmp_path, {})
    observed, done = [], []
    result = "no prefix available"

    def compact(*args, emit, **kwargs):
        if stage is not None:
            emit("compaction_notice", stage=stage, detail=detail)
        emit("post_compact", marker=True)
        return result

    monkeypatch.setattr(cli.compaction, "compact_now_sync", compact)
    monkeypatch.setattr(cli.compaction, "printing", lambda *args: lambda event, **payload: observed.append((event, payload)))
    monkeypatch.setattr(cli.msgs, "say", lambda message, **fields: done.append((message, fields)))
    _run_compact_route(monkeypatch, cfg, route)
    assert bool(done) is done_expected
    if done:
        assert done == [(cli.msgs.COMPACTION_DONE, {"result": result})]
    assert observed[-1] == ("post_compact", {"marker": True})


def _run_compact_route(monkeypatch, cfg, route):
    if route == "manual":
        monkeypatch.setattr(ReplState, "config_for_turn", lambda state, cfg: cfg)
        monkeypatch.setattr(repl_state, "repl_assembly", lambda state: SimpleNamespace(compact_force_focus="forced"))
        assert repl_session_commands._cmd_compact("", ReplState(system="", messages=[]), cfg) is None
    else:
        cfg.session_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.session_file.touch()
        monkeypatch.setattr(run_assembly, "from_env", lambda *args, **kwargs: cfg)
        monkeypatch.setattr(cli.P, "load_configured_prompt_spec", lambda cfg: SimpleNamespace(system=""))
        monkeypatch.setattr(cli.M, "load_replay_messages", lambda path: [])
        assert cli.main(["--compact", "test-session"]) == 0


@pytest.mark.parametrize("route", ["manual", "offline"])
def test_real_empty_compaction_publishes_skip_once(monkeypatch, tmp_path, capsys, route):
    cfg = _compact_test_cfg(tmp_path, {"COMPACTION_LOG_DIR": str(tmp_path / "flights")})
    details = []
    printing = cli.compaction.printing

    def track_printing(*args):
        emit = printing(*args)

        def tracked(event, **payload):
            if event == "compaction_notice" and payload.get("stage") == "skipped":
                details.append(payload["detail"])
            return emit(event, **payload)

        return tracked

    monkeypatch.setattr(cli.compaction, "printing", track_printing)
    _run_compact_route(monkeypatch, cfg, route)
    assert len(details) == 1
    captured = capsys.readouterr()
    assert (captured.out + captured.err).count(details[0]) == 1


def test_missing_compact_session_does_not_create_file_or_run_work(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.P, "load_configured_prompt_spec", lambda cfg: pytest.fail("loaded prompt for missing session"))
    monkeypatch.setattr(cli.compaction, "compact_now_sync", lambda *a, **k: pytest.fail("compacted missing session"))
    monkeypatch.setattr(cli.compaction, "flight_log_dir", lambda cfg: pytest.fail("opened flight for missing session"))

    assert cli.main(["--compact", "never-existed"]) != 0
    captured = capsys.readouterr()
    assert "never-existed" in captured.err
    assert not list(paths.sessions_root().rglob("never-existed.jsonl"))
    assert not list(paths.state_root().rglob("latest.json"))
    assert not list(paths.logs_root().rglob("compactions"))


def test_compact_session_removed_after_resolution_fails_before_prompt(monkeypatch, tmp_path, capsys):
    cfg = _compact_test_cfg(tmp_path, {})
    assert not cfg.session_file.exists()
    monkeypatch.setattr(run_assembly, "from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(cli.P, "load_configured_prompt_spec", lambda cfg: pytest.fail("loaded prompt after session vanished"))
    assert cli.main(["--compact", "vanished"]) != 0
    captured = capsys.readouterr()
    assert "vanished" in captured.err or str(cfg.session_file) in captured.err
    assert not cfg.session_file.exists()


@pytest.mark.parametrize("external", ["configured-summarizer", "same", ""])
@pytest.mark.parametrize("explicit", ["operator-summarizer", None])
def test_offline_compaction_explicit_model_wins_without_rewriting_config(monkeypatch, tmp_path, external, explicit):
    cfg = _compact_test_cfg(tmp_path, {"COMPACTION_EXTERNAL_MODEL": external})
    cfg.session_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.session_file.touch()
    observed = []

    def compact(actual_cfg, system, messages, *, model=None, **kwargs):
        observed.append((actual_cfg.model, model or compaction_settings.get_model(actual_cfg)))
        return "summary complete"

    monkeypatch.setattr(run_assembly, "from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(cli.P, "load_configured_prompt_spec", lambda cfg: SimpleNamespace(system=""))
    monkeypatch.setattr(cli.compaction, "compact_now_sync", compact)
    assert cli.main(["--compact", "existing", *(["--model", explicit] if explicit else [])]) == 0
    expected = explicit or (external if external not in {"", "same"} else cfg.model)
    assert observed == [(cfg.model, expected)]


@pytest.mark.parametrize("flags, expected", [
    ([], {"SHELL_WAIT": 41, "FETCH_TIMEOUT": 42, "SUBAGENT_DEPTH_MAX": None}),
    (["--preset", "deep"], {"SHELL_WAIT": 41, "FETCH_TIMEOUT": 42, "SUBAGENT_DEPTH_MAX": 7}),
    (["--no-local-config"], {"SHELL_WAIT": 41, "FETCH_TIMEOUT": None, "SUBAGENT_DEPTH_MAX": None}),
    (["--no-global-config"], {"SHELL_WAIT": None, "FETCH_TIMEOUT": 42, "SUBAGENT_DEPTH_MAX": None}),
])
def test_compact_loads_the_jsrc_stack_its_command_line_names(monkeypatch, tmp_path, flags, expected):
    from js import session_store, settings

    (tmp_path / ".js").mkdir()
    (tmp_path / ".js" / "jsrc").write_text("set SHELL_WAIT 41\n", encoding="utf-8")
    (tmp_path / ".js" / "jsrc.deep").write_text("set SUBAGENT_DEPTH_MAX 7\n", encoding="utf-8")
    project = tmp_path / "project"
    (project / ".js").mkdir(parents=True)
    (project / ".js" / "jsrc").write_text("set FETCH_TIMEOUT 42\n", encoding="utf-8")
    monkeypatch.chdir(project)
    session_file = session_store.folder_for(project) / "compact-session.jsonl"
    session_file.parent.mkdir(parents=True)
    cli.M.append_message(session_file, {"role": "user", "content": "old"})
    seen = []

    def compact(cfg, system, messages, **kwargs):
        seen.append({key: settings.knob(cfg.settings, key) for key in expected})
        return "summary complete"

    monkeypatch.setattr(cli.compaction, "compact_now_sync", compact)

    assert cli.main(["--compact", "compact-session", *flags]) == 0

    defaults = {key: settings.default_value(key) for key in expected}
    assert seen == [{key: defaults[key] if value is None else value for key, value in expected.items()}]
