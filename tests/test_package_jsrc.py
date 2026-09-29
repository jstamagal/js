"""js/jsrc, shipped in the package, is the built-in default layer.

Every registered knob starts at the value that file gives it. Changing a line
there changes what runs and what /show reports; without the file js does not
start.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from js import cli, setcmd, settings
from js.config import Config, from_env
from js.toolkit import ToolContext, kernel

REPO = Path(__file__).resolve().parents[1]


def _set_lines(text: str) -> list[str]:
    return [line.split()[1] for line in text.splitlines() if line.startswith("set ")]


def _package_copy(tmp_path: Path, monkeypatch, **changes: str) -> Path:
    """A copy of js/jsrc with ``changes`` (key -> raw value) applied, installed
    as the default layer for this test."""
    lines = settings.PACKAGE_JSRC.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        parts = line.split()
        key = parts[1].lstrip("-") if len(parts) >= 2 and parts[0] == "set" else None
        out.append(f"set {key} {changes[key]}" if key in changes else line)
    path = tmp_path / "pkg" / "jsrc"
    path.parent.mkdir()
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    monkeypatch.setattr(settings, "PACKAGE_JSRC", path)
    return path


def _isolated_home(monkeypatch, tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for spec in settings.REGISTRY:
        for name in settings.env_names_for(spec):
            monkeypatch.delenv(name, raising=False)
    return project


def test_package_jsrc_sets_every_registered_knob_exactly_once():
    keys = [key.lstrip("-") for key in _set_lines(settings.PACKAGE_JSRC.read_text(encoding="utf-8"))]

    assert sorted(keys) == sorted(spec.key for spec in settings.REGISTRY)


def test_seeded_settings_are_what_package_jsrc_sets():
    seeded = settings.seed_defaults()
    text = settings.PACKAGE_JSRC.read_text(encoding="utf-8")
    applied: dict = {}
    for line in text.splitlines():
        assert setcmd.apply_config_line(applied, line).error is None

    assert seeded == applied
    seeded["limits"]["max_read_lines"] = -1
    assert settings.default_value("limits.max_read_lines") != -1


def test_a_number_changed_in_package_jsrc_changes_behaviour_and_show(monkeypatch, tmp_path, capsys):
    _package_copy(tmp_path, monkeypatch, **{"kernel.wait_seconds": "9", "limits.max_read_lines": "17"})
    project = _isolated_home(monkeypatch, tmp_path)

    context = ToolContext(cwd=tmp_path)
    assert kernel.wait_seconds(context) == 9.0
    assert context.max_read_lines == 17

    cfg = from_env(save_session=False, cwd=project)
    assert cfg.kernel_wait_seconds == 9
    assert cfg.max_read_lines == 17

    state = {"messages": [], "system": "sys", "settings": settings.seed_defaults()}
    capsys.readouterr()
    assert cli._handle_command("/show kernel.wait_seconds", state, cfg) is True
    shown = capsys.readouterr().out.splitlines()[0]
    assert shown.split("=", 1)[1].split()[0] == "9"


def test_user_jsrc_layers_over_package_jsrc(monkeypatch, tmp_path):
    _package_copy(tmp_path, monkeypatch, **{"limits.fetch_timeout_s": "44", "limits.max_read_lines": "17"})
    project = _isolated_home(monkeypatch, tmp_path)
    user = tmp_path / "config" / "js" / "jsrc"
    user.parent.mkdir(parents=True)
    user.write_text("set limits.max_read_lines 33\n", encoding="utf-8")

    cfg = from_env(save_session=False, cwd=project)

    assert cfg.fetch_timeout_s == 44
    assert cfg.max_read_lines == 33


def test_missing_package_jsrc_stops_startup_with_one_line_naming_the_path(tmp_path):
    missing = tmp_path / "gone" / "jsrc"
    script = (
        "import runpy, sys\n"
        "from pathlib import Path\n"
        "import js.settings\n"
        "js.settings.PACKAGE_JSRC = Path(sys.argv[1])\n"
        "sys.argv = ['js', '-p', 'hello']\n"
        "runpy.run_module('js', run_name='__main__')\n"
    )
    env = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}

    proc = subprocess.run(
        [sys.executable, "-c", script, str(missing)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )

    assert proc.returncode != 0
    lines = proc.stderr.strip().splitlines()
    assert len(lines) == 1
    assert str(missing) in lines[0]


def test_bad_line_in_package_jsrc_names_the_file_and_line(monkeypatch, tmp_path):
    path = tmp_path / "jsrc"
    path.write_text("set model.id m\nset limits.max_read_lines lots\n", encoding="utf-8")
    monkeypatch.setattr(settings, "PACKAGE_JSRC", path)

    with pytest.raises(SystemExit) as raised:
        settings.seed_defaults()

    assert f"{path}:2" in str(raised.value)


def test_set_dash_in_a_jsrc_clears_a_lower_layer(tmp_path):
    global_cfg = tmp_path / "global"
    project_cfg = tmp_path / "project"
    global_cfg.write_text("set model.max_output_tokens 5000\n", encoding="utf-8")
    project_cfg.write_text("set -model.max_output_tokens\nset -limits.max_read_lines\n", encoding="utf-8")

    out = settings.collect_settings(config_paths=[global_cfg, project_cfg], env={})

    assert settings.get_dotted(out, ("model", "max_output_tokens")) is None
    assert settings.get_dotted(out, ("limits", "max_read_lines")) is None


def test_save_round_trips_a_cleared_knob_that_package_jsrc_sets(tmp_path):
    live = settings.seed_defaults()
    setcmd.set_command(live, "-limits.max_read_lines")
    setcmd.set_command(live, "limits.fetch_timeout_s 77")
    target = tmp_path / "jsrc"

    settings.save_settings_to_jsrc(target, live, stamp="t")
    reloaded = settings.collect_settings(config_paths=[target], env={})

    assert settings.get_dotted(reloaded, ("limits", "max_read_lines")) is None
    assert reloaded["limits"]["fetch_timeout_s"] == 77
    assert settings.settings_diff_lines(settings.seed_defaults()) == []


def test_config_and_tool_context_defaults_come_from_package_jsrc(monkeypatch, tmp_path):
    _package_copy(
        tmp_path, monkeypatch,
        **{"limits.browse_timeout_s": "61", "shell.wait_seconds": "31", "tools.terminal_cols": "70"},
    )
    d = tmp_path / "sessions"
    cfg = Config(
        agent_id="a", agent_dir=d, model="m", provider_id=None, provider_base_url=None,
        provider_api_key=None, reasoning_effort=None, max_output_tokens=None,
        max_tool_iterations=5, max_bash_output_bytes=1, max_tool_result_bytes=1,
        fetch_timeout_s=1, debug_log=None, trace=False, history_file=d / ".history",
        sessions_dir=d, session_file=d / "s.jsonl", prompts_dir=tmp_path,
    )
    context = ToolContext(cwd=tmp_path)

    assert cfg.browse_timeout_s == 61
    assert cfg.shell_wait_seconds == 31
    assert context.browse_timeout_s == 61
    assert context.shell_wait_seconds == 31
    assert context.terminal_cols == 70


def test_fetch_sends_the_configured_user_agent(monkeypatch, tmp_path):
    from js.toolkit import process_net

    seen = {}

    class Headers(dict):
        def get(self, key, default=None):
            return next((v for k, v in self.items() if k.lower() == str(key).lower()), default)

    class Response:
        headers = Headers({"Content-Type": "text/plain"})

        def read(self, size=-1):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout):
        seen["agent"] = req.get_header("User-agent")
        return Response()

    monkeypatch.setattr(process_net, "resolve_binary", lambda _name: None)
    monkeypatch.setattr(process_net.urllib.request, "urlopen", fake_urlopen)
    context = ToolContext(cwd=tmp_path, user_agent="knob-agent/2")

    process_net.fetch("https://example.test/", context=context)

    assert seen["agent"] == "knob-agent/2"
