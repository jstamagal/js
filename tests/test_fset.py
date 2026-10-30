"""js/fset.py: every line js prints goes through a rewritable /fset format,
and every colour is a 24-bit entry of the theme under the operator's own."""

from __future__ import annotations

import copy

from js import fset, setcmd, settings
from js import messages as msgs


def test_holes_fill_in_order_and_a_missing_one_is_empty():
    assert fset.render("%s and %s and %s", ["a", "b"], colour=False) == "a and b and"
    assert fset.render("no holes", ["dropped"], colour=False) == "no holes"
    assert fset.render("100%% %s", ["x"], colour=False) == "100% x"


def test_a_set_format_is_what_the_message_shows():
    assert setcmd.fset_command({}, "MODEL_SET model is now %s!").error is None
    assert msgs.MODEL_SET.line(colour=False, model="m1") == "model is now m1!"
    assert msgs.MODEL_SET.text(model="m1") == "Model: m1"


def test_fset_lines_survive_save_and_load(tmp_path):
    live = settings.seed_defaults()
    setcmd.fset_command(live, "THEME paper")
    setcmd.fset_command(live, "MODEL_SET now on %s")
    setcmd.fset_command(live, "TOOLS_DISPLAY 2")
    path = tmp_path / "jsrc"
    settings.save_changes(path, settings.seed_defaults(), live, fset_before={})
    fset.reset()

    reloaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], reloaded) == []
    assert fset.theme_name() == "paper"
    assert msgs.MODEL_SET.line(colour=False, model="m") == "now on m"
    assert settings.knob(reloaded, "TOOLS_DISPLAY") == 2


def test_an_old_display_setting_line_renames_to_an_fset_line():
    from js.rename import rename_old_names

    assert rename_old_names("set ui.net 0")[0] == "fset NET_DISPLAY 0"
    assert rename_old_names("set theme.accent #1fb4ff")[0] == "fset COLOR_TOOL #1fb4ff"
    assert rename_old_names("set model.id x")[0] == "set MODEL x"


def _saved_then_reloaded(path, change):
    """Load the jsrc at ``path``, run ``change`` on the live settings, /save,
    and load the saved file again from scratch."""
    live = settings.seed_defaults()
    settings.load_jsrc_files([path], live)
    before, fset_before = copy.deepcopy(live), fset.overrides()
    change(live)
    settings.save_changes(path, before, live, fset_before=fset_before)
    fset.reset()
    reloaded = settings.seed_defaults()
    settings.load_jsrc_files([path], reloaded)
    return reloaded


def test_a_cleared_entry_set_twice_stays_cleared_after_save(tmp_path):
    stock = fset.theme_name()
    path = tmp_path / "jsrc"
    path.write_text("fset THEME paper\nfset THEME github\n", encoding="utf-8")
    _saved_then_reloaded(path, lambda live: setcmd.fset_command(live, "-THEME"))
    assert fset.theme_name() == stock


def test_a_cleared_entry_a_loaded_file_set_stays_cleared_after_save(tmp_path):
    stock = fset.theme_name()
    (tmp_path / "child").write_text("fset THEME paper\n", encoding="utf-8")
    path = tmp_path / "jsrc"
    path.write_text("load child\n", encoding="utf-8")
    _saved_then_reloaded(path, lambda live: setcmd.fset_command(live, "-THEME"))
    assert fset.theme_name() == stock


def test_save_leaves_the_live_banner_as_it_is(tmp_path):
    (tmp_path / "child").write_text("set BANNER old\n", encoding="utf-8")
    path = tmp_path / "jsrc"
    path.write_text("load child\n", encoding="utf-8")
    live = settings.seed_defaults()
    settings.load_jsrc_files([path], live)
    before, fset_before = copy.deepcopy(live), fset.overrides()
    setcmd.set_command(live, "-BANNER")
    stock = msgs.BANNER
    settings.save_changes(path, before, live, fset_before=fset_before)
    assert msgs.BANNER == stock != "old"


def test_a_format_on_two_lines_survives_save_and_a_fresh_load(tmp_path):
    path = tmp_path / "jsrc"
    live = settings.seed_defaults()
    assert fset.set_entry("MODEL_SET", "first %s\nsecond") is None
    settings.save_changes(path, settings.seed_defaults(), live, fset_before={})
    fset.reset()
    settings.load_jsrc_files([path], settings.seed_defaults())
    assert fset.lookup("MODEL_SET") == "first %s\nsecond"


def test_an_entry_set_to_nothing_stays_set_to_nothing_after_save(tmp_path):
    path = tmp_path / "jsrc"
    live = settings.seed_defaults()
    assert setcmd.fset_command(live, 'MODEL_SET ""').error is None
    settings.save_changes(path, settings.seed_defaults(), live, fset_before={})
    fset.reset()
    settings.load_jsrc_files([path], settings.seed_defaults())
    assert fset.lookup("MODEL_SET") == ""


def test_a_theme_file_reads_its_lines_as_a_jsrc_does(tmp_path):
    path = tmp_path / "theme"
    path.write_text('fset\tMARK_GAP\t2\nfset MARK_USER ""\nfset MODEL_SET "two\\nlines %s"\n', encoding="utf-8")
    assert fset.read_file(path) == {"MARK_GAP": "2", "MARK_USER": "", "MODEL_SET": "two\nlines %s"}
