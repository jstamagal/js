"""The skill picker: `/skills` lists every skill and switches each on or off;
the result is the SKILLS_DISABLED setting. The app is driven through a pipe
input, as the model picker's tests do. Every test runs in the tmp HOME the
conftest installs."""

from __future__ import annotations




from js import cli, settings, skill_picker

from test_command_table import _launch, _run


def test_skills_command_writes_the_picked_names_into_the_setting(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg, state, _errors = _launch(tmp_path)
    monkeypatch.setattr(skill_picker, "pick_skills", lambda cwd, disabled: ("alpha",))

    _run(["/skills"], state, cfg)

    assert settings.knob(state["settings"], "SKILLS_DISABLED") == ["alpha"]
    assert cli._cfg_for_live_state(cfg, state).skills_disabled == ("alpha",)


def test_skill_picker_accepts_its_view_and_streams(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from js import picker, skills

    source, target = object(), object()
    seen = []
    monkeypatch.setattr(skills, "discover_skills", lambda path: SimpleNamespace(skills=()))
    state = SimpleNamespace(application=lambda **kwargs: seen.append(kwargs) or "app")
    monkeypatch.setattr(picker, "run_modal", lambda app: app)
    assert skill_picker.pick_skills(tmp_path, input=source, output=target,
                                    picker_factory=lambda *args: state) == "app"
    assert seen == [{"input": source, "output": target}]
