"""js/jsrc, shipped in the package, is the built-in default layer.

Every registered knob starts at the value that file gives it. Changing a line
there changes what runs and what /show reports; without the file js does not
start.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from js import cli, setcmd, settings
from js.config import Config, from_env
from js.toolkit import ToolContext, kernel

REPO = Path(__file__).resolve().parents[1]


def _set_lines(text: str) -> list[str]:
    return [line.split()[1] for line in text.splitlines() if line.startswith(("set ", "fset "))]


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
    seeded["READ_LINES_MAX"] = -1
    assert settings.default_value("READ_LINES_MAX") != -1


def test_a_number_changed_in_package_jsrc_changes_behaviour_and_show(monkeypatch, tmp_path, capsys):
    _package_copy(tmp_path, monkeypatch, **{"PYTHON_KERNEL_WAIT": "9", "READ_LINES_MAX": "17"})
    project = _isolated_home(monkeypatch, tmp_path)

    context = ToolContext(cwd=tmp_path)
    assert kernel.wait_seconds(context) == 9.0
    assert context.max_read_lines == 17

    cfg = from_env(save_session=False, cwd=project)
    assert cfg.kernel_wait_seconds == 9
    assert cfg.max_read_lines == 17

    state = {"messages": [], "system": "sys", "settings": settings.seed_defaults()}
    capsys.readouterr()
    assert cli._handle_command("/show PYTHON_KERNEL_WAIT", state, cfg) is True
    shown = capsys.readouterr().out.splitlines()[0]
    assert shown.split(": ", 1)[1].split()[0] == "9"


def test_user_jsrc_layers_over_package_jsrc(monkeypatch, tmp_path):
    _package_copy(tmp_path, monkeypatch, **{"FETCH_TIMEOUT": "44", "READ_LINES_MAX": "17"})
    project = _isolated_home(monkeypatch, tmp_path)
    user = tmp_path / "home" / ".js" / "jsrc"
    user.parent.mkdir(parents=True)
    user.write_text("set READ_LINES_MAX 33\n", encoding="utf-8")

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


def test_first_run_writes_the_commented_template_and_leaves_an_existing_jsrc(monkeypatch, tmp_path):
    project = _isolated_home(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    user = tmp_path / "home" / ".js" / "jsrc"

    assert cli.main(["--list"]) == 0
    assert user.read_text(encoding="utf-8") == settings.JSRC_TEMPLATE.read_text(encoding="utf-8")
    assert settings.collect_settings(config_paths=[user], env={}) == settings.seed_defaults()

    user.write_text("set READ_LINES_MAX 33\n", encoding="utf-8")
    assert cli.main(["--list"]) == 0
    assert user.read_text(encoding="utf-8") == "set READ_LINES_MAX 33\n"


def _show_value(cfg, state, key: str, capsys) -> str:
    capsys.readouterr()
    assert cli._handle_command(f"/show {key}", state, cfg) is True
    return capsys.readouterr().out.splitlines()[0].split(": ", 1)[1].split()[0]


def test_set_dash_in_a_user_jsrc_runs_and_shows_the_package_value(monkeypatch, tmp_path, capsys):
    project = _isolated_home(monkeypatch, tmp_path)
    user = tmp_path / "home" / ".js" / "jsrc"
    user.parent.mkdir(parents=True)
    user.write_text(
        "set TRANSCRIPT_LOG off\nset READ_LINES_MAX 33\nset INLINE_CODE off\n"
        "set -TRANSCRIPT_LOG\nset -READ_LINES_MAX\nset -INLINE_CODE\n",
        encoding="utf-8",
    )

    cfg = from_env(save_session=False, cwd=project)
    state = {"messages": [], "system": "sys", "settings": copy.deepcopy(cfg.settings)}

    assert cfg.transcript_log is settings.default_value("TRANSCRIPT_LOG")
    assert cfg.max_read_lines == settings.default_value("READ_LINES_MAX")
    assert cfg.allow_inline_code is settings.default_value("INLINE_CODE")
    assert _show_value(cfg, state, "TRANSCRIPT_LOG", capsys) == (
        "on" if cfg.transcript_log else "off"
    )
    assert _show_value(cfg, state, "READ_LINES_MAX", capsys) == str(cfg.max_read_lines)
    assert _show_value(cfg, state, "INLINE_CODE", capsys) == (
        "on" if cfg.allow_inline_code else "off"
    )


def test_live_set_dash_returns_to_the_session_start_value_in_show_turn_and_save(
    monkeypatch, tmp_path, capsys,
):
    project = _isolated_home(monkeypatch, tmp_path)
    user = tmp_path / "home" / ".js" / "jsrc"
    user.parent.mkdir(parents=True)
    user.write_text("set READ_LINES_MAX 33\n", encoding="utf-8")
    cfg = from_env(save_session=False, cwd=project)
    state = {"messages": [], "system": "sys", "settings": copy.deepcopy(cfg.settings)}

    assert cli._handle_command("/set READ_LINES_MAX 5", state, cfg) is True
    assert cli._handle_command("/set -READ_LINES_MAX", state, cfg) is True

    assert _show_value(cfg, state, "READ_LINES_MAX", capsys) == "33"
    assert cli._cfg_for_live_state(cfg, state).max_read_lines == 33
    assert cli._handle_command("/save", state, cfg) is True
    reloaded = from_env(save_session=False, cwd=project)
    assert reloaded.max_read_lines == 33


def test_save_uncomments_changed_settings_in_place_and_leaves_the_rest(tmp_path):
    target = tmp_path / "jsrc"
    settings.ensure_user_jsrc(target)
    target.write_text(target.read_text(encoding="utf-8") + "# my note\n", encoding="utf-8")
    before = target.read_text(encoding="utf-8").splitlines()
    live = settings.seed_defaults()
    setcmd.set_command(live, "FETCH_TIMEOUT 77")
    setcmd.set_command(live, "PROVIDER deepseek")

    assert settings.save_settings_to_jsrc(target, live) == 2
    after = target.read_text(encoding="utf-8").splitlines()
    changed = {before[i]: after[i] for i in range(len(before)) if before[i] != after[i]}

    assert len(after) == len(before)
    assert changed == {
        f"#set FETCH_TIMEOUT {settings.default_value('FETCH_TIMEOUT')}": "set FETCH_TIMEOUT 77",
        "#set -PROVIDER": "set PROVIDER deepseek",
    }
    assert settings.collect_settings(config_paths=[target], env={}) == live
    assert [path.name for path in tmp_path.glob("jsrc*")] == ["jsrc"]


def test_jsrc_tool_knobs_reach_the_turn_tool_context(monkeypatch, tmp_path):
    import ai.types.messages
    import ai.types.usage

    from js import runtime
    from js.model_client import ModelStreamResult
    from js.toolkit import build_default_registry

    project = _isolated_home(monkeypatch, tmp_path)
    user = tmp_path / "home" / ".js" / "jsrc"
    user.parent.mkdir(parents=True)
    user.write_text(
        "set HTTP_USER_AGENT knob-agent/3\nset TERMINAL_COLUMNS 71\nset TERMINAL_ROWS 29\n"
        "set SHELL_PROGRAM zsh\n",
        encoding="utf-8",
    )
    cfg = from_env(save_session=False, cwd=project)
    reply = ModelStreamResult(
        text="ok", tool_calls=[], reasoning="",
        usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1), finish_reason="stop",
        assistant_message=ai.types.messages.Message(role="assistant", parts=[ai.types.messages.TextPart(text="ok")]),
    )
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **_kwargs: reply)
    context = ToolContext(cwd=tmp_path)

    runtime.run_turn(
        cfg, "system", [{"role": "user", "content": "hi"}], runtime.Telemetry(None),
        trace_override=False, tool_registry=build_default_registry().select([]),
        tool_context=context, suppress_output=True,
    )

    assert context.user_agent == "knob-agent/3"
    assert context.terminal_cols == 71
    assert context.terminal_rows == 29
    assert context.shell_program == "zsh"


def test_config_and_tool_context_defaults_come_from_package_jsrc(monkeypatch, tmp_path):
    _package_copy(
        tmp_path, monkeypatch,
        **{"BROWSE_TIMEOUT": "61", "SHELL_WAIT": "31", "TERMINAL_COLUMNS": "70"},
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


def test_text_attachment_cap_follows_its_knob(monkeypatch, tmp_path):
    from js import attach

    project = _isolated_home(monkeypatch, tmp_path)
    user = tmp_path / "home" / ".js" / "jsrc"
    user.parent.mkdir(parents=True)
    user.write_text("set ATTACHMENT_MAX 10\n", encoding="utf-8")
    cfg = from_env(save_session=False, cwd=project)
    (project / "a.txt").write_text("q" * 100, encoding="utf-8")

    content = attach.build_user_message("look", ["a.txt"], cfg, cwd=project)["content"]

    assert "q" * 10 in content
    assert "q" * 11 not in content


@pytest.mark.parametrize(("max_files", "reattached"), [("0", False), ("5", True)])
def test_rehydrate_max_files_knob_decides_whether_files_come_back(
    monkeypatch, tmp_path, max_files, reattached,
):
    from types import SimpleNamespace

    from js import compaction

    project = _isolated_home(monkeypatch, tmp_path)
    user = tmp_path / "home" / ".js" / "jsrc"
    user.parent.mkdir(parents=True)
    user.write_text(
        f"set MODEL offline-test-model\nset COMPACTION_REATTACH_FILES_MAX {max_files}\n", encoding="utf-8",
    )
    cfg = replace(from_env(save_session=False, cwd=project), session_file=tmp_path / "s.jsonl")
    source = project / "kept.py"
    source.write_text("print('rehydrated-marker')\n", encoding="utf-8")
    context = ToolContext(cwd=project)
    context.read_paths = {source}

    async def stream_stub(**_kwargs):
        return SimpleNamespace(text="Summary")

    monkeypatch.setattr(compaction.model_client, "stream_model_async", stream_stub)
    messages = [
        {"role": "user", "content": "old " * 20000},
        {"role": "assistant", "content": "done"},
    ]

    result = compaction.compact_now_sync(cfg, "SYSTEM", messages, forced=True, context=context)

    assert cli.compaction.compacted(result)
    assert any("rehydrated-marker" in str(m.get("content")) for m in messages) is reattached


def test_collect_settings_can_preserve_operator_formats():
    from js import fset, settings

    settings.seed_defaults()
    fset.set_entry("MODEL_SET", "custom")
    before = dict(fset.overrides())
    settings.collect_settings(config_paths=[], env={}, reset_formats=lambda: None)
    assert dict(fset.overrides()) == before
