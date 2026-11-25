"""The skill picker: `/skills` lists every skill and switches each on or off;
the result is the SKILLS_DISABLED setting. The app is driven through a pipe
input, as the model picker's tests do. Every test runs in the tmp HOME the
conftest installs."""

from __future__ import annotations

from repl_driver import record_turns, type_into_js

from js import settings, skill_picker


def test_skill_picker_accepts_context_keymaps(tmp_path):
    from js import skills

    root = tmp_path / ".agents" / "skills" / "alpha"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("instructions")
    state = skill_picker.SkillPicker(skills.discover_skills(tmp_path).skills,
                                    keymaps={"list": {"toggle": (("z",),), "choose": (("enter",),)}})
    state.press(" ")
    state.press("z")
    assert state.press("enter") == ("alpha",)


def test_skills_command_writes_the_picked_names_into_the_setting(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(skill_picker, "pick_skills", lambda cwd, disabled: ("alpha",))
    turns = record_turns(monkeypatch)

    assert type_into_js(monkeypatch, ["/skills", "hello"]) == 0

    [turn] = turns
    assert settings.knob(turn.settings, "SKILLS_DISABLED") == ["alpha"]
    assert turn.cfg.skills_disabled == ("alpha",)


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
