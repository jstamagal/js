"""js/fset.py: every line js prints goes through a rewritable /fset format,
and every colour is a 24-bit entry of the theme under the operator's own."""

from __future__ import annotations

import copy

from js import fset, setcmd, settings
from js import messages as msgs


def test_holes_name_words_of_the_line_in_any_order():
    line = ["503", "127.0.0.1", "The server is unavailable"]
    assert fset.render("$1 said $0", line, colour=False) == "127.0.0.1 said 503"
    assert fset.render("$2-", line, colour=False) == "The server is unavailable"
    assert fset.render("$3", line, colour=False) == "server"
    assert fset.render("$*", line, colour=False) == "503 127.0.0.1 The server is unavailable"
    assert fset.render("$7 and $7-|", line, colour=False) == " and |"
    assert fset.render("no holes", line, colour=False) == "no holes"
    assert fset.render("100%% $$5 $0", ["x"], colour=False) == "100% $5 x"


def test_a_field_before_the_last_is_one_word_whatever_it_holds():
    assert fset.render("[$0] [$1-]", ["a path", "b c"], colour=False) == "[a path] [b c]"


def test_a_colour_code_paints_what_follows_and_bare_c_ends_it():
    red = fset.hex_for("PALETTE_04")
    assert fset.runs("%C04%G%C Unknown setting: %C04$0%C", fset.words(["X"])) == [
        (red, msgs.current_banner()), ("", " Unknown setting: "), (red, "X")]


def test_a_format_that_names_a_code_or_word_its_event_lacks_is_refused():
    for bad in ("model is %s", "$NOPE", "%C99", "$1", "100%"):
        assert setcmd.fset_command({}, f"MODEL_SET {bad}").error is not None, bad
    assert setcmd.fset_command({}, "NO_ALIASES none: $0").error is not None
    assert setcmd.fset_command({}, "MODEL_SET %C04$0-%C is $* $$ 100%%").error is None


def test_a_set_format_is_what_the_message_shows():
    assert setcmd.fset_command({}, "MODEL_SET model is now $0!").error is None
    assert msgs.MODEL_SET.line(colour=False, model="m1") == "model is now m1!"
    assert msgs.MODEL_SET.text(model="m1") == "Model: m1"


def test_fset_lines_survive_save_and_load(tmp_path):
    live = settings.seed_defaults()
    setcmd.fset_command(live, "THEME paper")
    setcmd.fset_command(live, "MODEL_SET now on $0-")
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
    assert fset.set_entry("MODEL_SET", "first $0\nsecond") is None
    settings.save_changes(path, settings.seed_defaults(), live, fset_before={})
    fset.reset()
    settings.load_jsrc_files([path], settings.seed_defaults())
    assert fset.lookup("MODEL_SET") == "first $0\nsecond"


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
    path.write_text('fset\tMARK_GAP\t2\nfset MARK_USER ""\nfset MODEL_SET "two\\nlines $0"\n', encoding="utf-8")
    assert fset.read_file(path) == {"MARK_GAP": "2", "MARK_USER": "", "MODEL_SET": "two\nlines $0"}
