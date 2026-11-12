"""Offline compaction CLI routing and notice regressions."""

import io
from types import SimpleNamespace

import pytest

from js import cli, paths
from test_config_compaction_layers import _compact_test_cfg


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
        monkeypatch.setattr(cli, "_cfg_for_live_state", lambda cfg, state: cfg)
        monkeypatch.setattr(cli, "_repl_assembly", lambda state: SimpleNamespace(compact_force_focus="forced"))
        assert cli._cmd_compact("", {"system": "", "messages": []}, cfg) is None
    else:
        cfg.session_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.session_file.touch()
        monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
        monkeypatch.setattr(cli.P, "load_configured_prompt_spec", lambda cfg: SimpleNamespace(system=""))
        monkeypatch.setattr(cli.M, "load_replay_messages", lambda path: [])
        assert cli._run_compact_offline("test-session") == 0


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

    assert cli._run_compact_offline("never-existed") != 0
    captured = capsys.readouterr()
    assert "never-existed" in captured.err
    assert not list(paths.sessions_root().rglob("never-existed.jsonl"))
    assert not list(paths.state_root().rglob("latest.json"))
    assert not list(paths.logs_root().rglob("compactions"))


def test_compact_session_removed_after_resolution_fails_before_prompt(monkeypatch, tmp_path, capsys):
    cfg = _compact_test_cfg(tmp_path, {})
    assert not cfg.session_file.exists()
    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(cli.P, "load_configured_prompt_spec", lambda cfg: pytest.fail("loaded prompt after session vanished"))
    assert cli._run_compact_offline("vanished") != 0
    captured = capsys.readouterr()
    assert "vanished" in captured.err or str(cfg.session_file) in captured.err
    assert not cfg.session_file.exists()
