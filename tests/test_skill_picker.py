"""The skill picker: `/skills` lists every skill and switches each on or off;
the result is the SKILLS_DISABLED setting. The app is driven through a pipe
input, as the model picker's tests do. Every test runs in the tmp HOME the
conftest installs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import DummyOutput

from js import cli, settings, skill_picker
from js.skills import SkillInvocationError, SkillMetadata

from test_command_table import _launch, _run


def _skill(name: str, source: str = "global") -> SkillMetadata:
    return SkillMetadata(name, f"{name} rules", (), source, Path(name) / "SKILL.md")


async def _drive(state: skill_picker.SkillPicker, *keys: str):
    """Run the picker app, typing each key in turn; return the app's result."""
    with create_pipe_input() as pipe:
        app = state.application(input=pipe, output=DummyOutput())
        task = asyncio.ensure_future(app.run_async())
        for key in keys:
            await asyncio.sleep(0.05)
            pipe.send_text(key)
        return await asyncio.wait_for(task, timeout=10)


def test_the_picker_toggles_skills_and_keeps_names_it_does_not_list():
    state = skill_picker.SkillPicker(
        [_skill("alpha"), _skill("beta", "project"), _skill("gamma", "builtin")],
        disabled=["gamma", "elsewhere"],
    )

    # down to beta, switch it off; down to gamma, switch it on; enter keeps.
    result = asyncio.run(_drive(state, "j", " ", "j", " ", "\r"))

    assert result == ("beta", "elsewhere")


def test_the_picker_closed_without_enter_changes_nothing():
    state = skill_picker.SkillPicker([_skill("alpha")], disabled=[])

    assert asyncio.run(_drive(state, " ", "q")) is None


def test_skills_command_writes_the_picked_names_into_the_setting(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg, state, _errors = _launch(tmp_path)
    monkeypatch.setattr(skill_picker, "pick_skills", lambda cwd, disabled: ("alpha",))

    _run(["/skills"], state, cfg)

    assert settings.knob(state["settings"], "SKILLS_DISABLED") == ["alpha"]
    assert cli._cfg_for_live_state(cfg, state).skills_disabled == ("alpha",)


def test_disabled_names_match_for_runtime_invocation_and_picker(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".js" / "skills" / "handoff" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("handoff instructions", encoding="utf-8")
    cfg, state, _errors = _launch(tmp_path)
    _run(['/set SKILLS_DISABLED [" HaNdOfF ", " elsewhere "]'], state, cfg)

    assert cli._cfg_for_live_state(cfg, state).skills_disabled == ("HaNdOfF", "elsewhere")
    with pytest.raises(SkillInvocationError):
        cli._expand_skill_line("/skill handoff", state)

    picker = skill_picker.SkillPicker([_skill("handoff")], disabled=cli._disabled_skills(state))
    picker.press(" ")
    assert picker.press("enter") == ("elsewhere",)


def test_long_skill_names_leave_sources_visible_when_the_terminal_resizes(monkeypatch):
    state = skill_picker.SkillPicker([_skill("short", "global"), _skill("z" * 80, "project")])
    output = DummyOutput()
    monkeypatch.setattr(output, "get_size", lambda: Size(rows=8, columns=width))
    with create_pipe_input() as pipe:
        app = state.application(input=pipe, output=output)
        with set_app(app):
            for width in (80, 40, 120):
                app.render_counter += 1
                screen = Screen()
                app.layout.container.write_to_screen(
                    screen, MouseHandlers(), WritePosition(0, 0, width, 8),
                    parent_style="", erase_bg=False, z_index=None,
                )
                screen.draw_all_floats()
                shown = [
                    "".join(screen.data_buffer[y][x].char for x in range(width))
                    for y in range(8)
                ]
                assert any("global" in line for line in shown)
                assert any("project" in line for line in shown)
