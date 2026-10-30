"""/save writes only what the operator changed in the session: no provider,
endpoint, key or default the environment, a login or js itself put in. Nothing
but /save writes the user jsrc."""

from __future__ import annotations

import copy
import os

from js import cli
from js.config import from_env


def _home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    for name in [name for name in os.environ if name.startswith("JS_")]:
        monkeypatch.delenv(name)
    project = tmp_path / "project"
    project.mkdir()
    return home / ".js" / "jsrc", project


def _session(project):
    cfg = from_env(save_session=False, cwd=project)
    state = {"messages": [], "system": "sys", "settings": copy.deepcopy(cfg.settings),
             "model": cfg.model, "provider_id": cfg.provider_id}
    cli._mark_saved(state)
    return cfg, state


def test_save_writes_only_the_setting_changed_this_session(monkeypatch, tmp_path):
    jsrc, project = _home(monkeypatch, tmp_path)
    monkeypatch.setenv("JS_BASE_URL", "http://env.test/v1")
    monkeypatch.setenv("JS_API_KEY", "env-key")
    cfg, state = _session(project)

    assert cli._handle_command("/set FETCH_TIMEOUT 77", state, cfg) is True
    assert cli._handle_command("/save", state, cfg) is True

    assert jsrc.read_text(encoding="utf-8") == "set FETCH_TIMEOUT 77\n"


def test_save_with_nothing_changed_writes_no_jsrc(monkeypatch, tmp_path):
    jsrc, project = _home(monkeypatch, tmp_path)
    monkeypatch.setenv("JS_BASE_URL", "http://env.test/v1")
    cfg, state = _session(project)

    assert cli._handle_command("/save", state, cfg) is True

    assert not jsrc.exists()


def test_a_second_save_adds_only_what_changed_after_the_first(monkeypatch, tmp_path):
    jsrc, project = _home(monkeypatch, tmp_path)
    cfg, state = _session(project)

    assert cli._handle_command("/set FETCH_TIMEOUT 77", state, cfg) is True
    assert cli._handle_command("/save", state, cfg) is True
    assert cli._handle_command("/set READ_LINES_MAX 5", state, cfg) is True
    assert cli._handle_command("/save", state, cfg) is True

    assert jsrc.read_text(encoding="utf-8") == "set FETCH_TIMEOUT 77\nset READ_LINES_MAX 5\n"


def test_a_line_the_operator_wrote_is_left_as_it_is(monkeypatch, tmp_path):
    jsrc, project = _home(monkeypatch, tmp_path)
    jsrc.parent.mkdir(parents=True)
    jsrc.write_text("# mine\nset READ_LINES_MAX 33\n", encoding="utf-8")
    cfg, state = _session(project)

    assert cli._handle_command("/set FETCH_TIMEOUT 77", state, cfg) is True
    assert cli._handle_command("/save", state, cfg) is True

    assert jsrc.read_text(encoding="utf-8") == "# mine\nset READ_LINES_MAX 33\nset FETCH_TIMEOUT 77\n"
    assert [path.name for path in jsrc.parent.glob("jsrc*")] == ["jsrc"]
