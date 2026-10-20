"""js/fset.py: every line js prints goes through a rewritable /fset format,
and every colour is a 24-bit entry of the theme under the operator's own."""

from __future__ import annotations

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


def test_banner_is_the_banner_setting(monkeypatch):
    monkeypatch.setattr(msgs, "BANNER", "-!-")
    assert msgs.MODEL_SET.line(colour=False, model="m") == "-!- Model: m"
    assert fset.render("$G x", [], colour=False) == "-!- x"


def test_a_setting_in_a_format_reads_as_set_writes_it():
    live = settings.seed_defaults()
    settings.set_dotted(live, ("MARKDOWN",), False)
    settings.set_dotted(live, ("TOOLS_QUIET",), ["read"])
    fset.bind_settings(live)
    assert fset.render("$MARKDOWN $TOOLS_QUIET", [], colour=False) == 'off ["read"]'


def test_a_palette_slot_is_sent_as_24_bit():
    assert fset.render("%C04x%C", [], colour=True).startswith("\x1b[38;2;255;0;0mx")
    assert setcmd.fset_command({}, "PALETTE_04 #102030").error is None
    assert fset.render("%C04x", [], colour=True).startswith("\x1b[38;2;16;32;48mx")


def test_the_operator_beats_the_theme_and_the_theme_beats_the_default():
    assert setcmd.fset_command({}, "THEME bitchx").error is None
    assert fset.hex_for("COLOR_STATUS_BACKGROUND") == "#00007f"
    assert fset.hex_for("COLOR_TEXT") == "#ffffff"
    assert setcmd.fset_command({}, "COLOR_TEXT #123456").error is None
    assert fset.hex_for("COLOR_TEXT") == "#123456"


def test_host_color_takes_the_marks_and_unset_leaves_the_theme_s_own():
    own = fset.hex_for("COLOR_USER_MARK")
    assert setcmd.fset_command({}, "HOST_COLOR #0000aa").error is None
    assert fset.hex_for("COLOR_USER_MARK") == fset.hex_for("COLOR_AGENT_MARK") == "#0000aa"
    assert setcmd.fset_command({}, "-HOST_COLOR").error is None
    assert fset.hex_for("COLOR_USER_MARK") == own


def test_every_theme_sets_every_role():
    for name in fset.themes():
        entries = fset.theme_entries(name)
        missing = [role for role in fset.ROLES if role not in entries and role not in fset.OPTIONAL_ROLES]
        assert missing == [], name


def test_display_levels_are_fset_entries_and_set_refuses_them():
    live = settings.seed_defaults()
    assert setcmd.set_command(live, "TOOLS_DISPLAY 3").error
    assert setcmd.fset_command(live, "TOOLS_DISPLAY 3").error is None
    assert settings.knob(live, "TOOLS_DISPLAY") == 3


def test_fset_lines_survive_save_and_load(tmp_path):
    live = settings.seed_defaults()
    setcmd.fset_command(live, "THEME paper")
    setcmd.fset_command(live, "MODEL_SET now on %s")
    setcmd.fset_command(live, "TOOLS_DISPLAY 2")
    path = tmp_path / "jsrc"
    settings.save_settings_to_jsrc(path, live)
    fset.reset()

    reloaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], reloaded) == []
    assert fset.theme_name() == "paper"
    assert msgs.MODEL_SET.line(colour=False, model="m") == "now on m"
    assert settings.knob(reloaded, "TOOLS_DISPLAY") == 2


def test_no_colour_js_draws_is_a_terminal_palette_index():
    """Every colour escape a message carries is 24-bit."""
    for theme in fset.themes():
        setcmd.fset_command({}, f"THEME {theme}")
        line = msgs.MODEL_NOT_SAVED_AS_DEFAULT.line(model="m", error="e")
        for code in fset._ESCAPE.findall(line):
            assert code == fset.RESET or code.startswith(("\x1b[38;2;", "\x1b[48;2;", "\x1b[1m")), (theme, code)


def test_an_old_display_setting_line_renames_to_an_fset_line():
    from js.rename import rename_old_names

    assert rename_old_names("set ui.net 0")[0] == "fset NET_DISPLAY 0"
    assert rename_old_names("set theme.accent #1fb4ff")[0] == "fset COLOR_TOOL #1fb4ff"
    assert rename_old_names("set model.id x")[0] == "set MODEL x"


def test_tool_chain_headings_draw_through_their_formats():
    from js.toolkit import policy

    live = settings.seed_defaults()
    assert setcmd.fset_command(live, "TOOL_CHAIN_TOOL Werkzeug").error is None
    assert "Werkzeug" in fset.plain(policy.render_table([])[0])


def _saved_then_reloaded(path, change):
    """Load the jsrc at ``path``, run ``change`` on the live settings, /save,
    and load the saved file again from scratch."""
    live = settings.seed_defaults()
    settings.load_jsrc_files([path], live)
    change(live)
    settings.save_settings_to_jsrc(path, live)
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
    setcmd.set_command(live, "-BANNER")
    stock = msgs.BANNER
    settings.save_settings_to_jsrc(path, live)
    assert msgs.BANNER == stock != "old"


def test_save_writes_a_clear_once(tmp_path):
    (tmp_path / "child").write_text("set BANNER old\n", encoding="utf-8")
    path = tmp_path / "jsrc"
    path.write_text("load child\n", encoding="utf-8")
    live = settings.seed_defaults()
    settings.load_jsrc_files([path], live)
    setcmd.set_command(live, "-BANNER")
    settings.save_settings_to_jsrc(path, live)
    assert path.read_text(encoding="utf-8").splitlines().count("set -BANNER") == 1


def test_a_format_on_two_lines_survives_save_and_a_fresh_load(tmp_path):
    path = tmp_path / "jsrc"
    live = settings.seed_defaults()
    assert fset.set_entry("MODEL_SET", "first %s\nsecond") is None
    settings.save_settings_to_jsrc(path, live)
    fset.reset()
    settings.load_jsrc_files([path], settings.seed_defaults())
    assert fset.lookup("MODEL_SET") == "first %s\nsecond"


def test_a_setting_a_line_cannot_hold_as_it_is_survives_save_and_a_fresh_load(tmp_path):
    path = tmp_path / "jsrc"
    live = settings.seed_defaults()
    for value in ("two\nlines $MODEL", "  padded  ", '"quoted"'):
        settings.set_dotted(live, settings.spec_for("BANNER").path, value)
        settings.save_settings_to_jsrc(path, live)
        reloaded = settings.seed_defaults()
        settings.load_jsrc_files([path], reloaded)
        assert settings.knob(reloaded, "BANNER") == value


_AWKWARD = ["", "  padded  ", "two\nlines", '"/usr/bin/printf" image', "https://x.test/a?b=$MODEL", "plain",
            "emacs", "cwd", "c-v", "high"]


def test_every_string_setting_means_the_same_typed_as_its_line_and_survives_save(tmp_path):
    for spec in settings.REGISTRY:
        if spec.type != "str":
            continue
        for value in _AWKWARD:
            typed, error = settings.coerce_value(spec, settings.line_literal(value))
            if value == value.strip() and "\n" not in value:
                assert (typed, error) == settings.coerce_value(spec, value), (spec.key, value)
            if error is not None:
                continue
            path = tmp_path / f"{spec.key}.jsrc"
            live = settings.seed_defaults()
            settings.set_dotted(live, spec.path, typed)
            settings.save_settings_to_jsrc(path, live)
            reloaded = settings.seed_defaults()
            settings.load_jsrc_files([path], reloaded)
            back = settings.knob(reloaded, spec.key)
            # An empty value and none are both the stock value of a setting with none.
            assert back == typed or (back in (None, "") and typed in (None, "") and spec.default in (None, "")), (
                spec.key, value, back)


def test_an_entry_set_to_nothing_stays_set_to_nothing_after_save(tmp_path):
    path = tmp_path / "jsrc"
    live = settings.seed_defaults()
    assert setcmd.fset_command(live, 'MODEL_SET ""').error is None
    settings.save_settings_to_jsrc(path, live)
    fset.reset()
    settings.load_jsrc_files([path], settings.seed_defaults())
    assert fset.lookup("MODEL_SET") == ""


def test_any_whitespace_separates_the_words_of_set_fset_and_a_jsrc_line(tmp_path):
    live = settings.seed_defaults()
    assert setcmd.fset_command(live, "TOOLS_DISPLAY\t3").error is None
    assert settings.knob(live, "TOOLS_DISPLAY") == 3
    path = tmp_path / "jsrc"
    path.write_text("fset\tTOOLS_DISPLAY\t2\nset\tHISTORY_SIZE\t9\n", encoding="utf-8")
    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], loaded) == []
    assert (settings.knob(loaded, "TOOLS_DISPLAY"), settings.knob(loaded, "HISTORY_SIZE")) == (2, 9)


def test_a_map_child_written_by_the_jsrc_encoder_loads_and_saves_as_it_was(tmp_path):
    for spec in settings.REGISTRY:
        if spec.type != "map":
            continue
        for value in ('"quoted" x', '"quoted"', "two\nlines", "  padded  ", "plain words", ""):
            path = tmp_path / f"{spec.key}.jsrc"
            path.write_text(f"set {spec.key}.note {settings.jsrc_text(spec.key + '.note', value)}\n",
                            encoding="utf-8")
            loaded = settings.seed_defaults()
            settings.load_jsrc_files([path], loaded)
            assert (settings.knob(loaded, spec.key) or {}).get("note") == value, (spec.key, value)
            saved = tmp_path / f"{spec.key}.saved"
            settings.save_settings_to_jsrc(saved, loaded)
            reloaded = settings.seed_defaults()
            settings.load_jsrc_files([saved], reloaded)
            assert (settings.knob(reloaded, spec.key) or {}).get("note") == value, (spec.key, value)
            saved.unlink()


def test_a_theme_file_reads_its_lines_as_a_jsrc_does(tmp_path):
    path = tmp_path / "theme"
    path.write_text('fset\tMARK_GAP\t2\nfset MARK_USER ""\nfset MODEL_SET "two\\nlines %s"\n', encoding="utf-8")
    assert fset.read_file(path) == {"MARK_GAP": "2", "MARK_USER": "", "MODEL_SET": "two\nlines %s"}


def test_mark_gap_takes_only_ascii_digits():
    live = settings.seed_defaults()
    assert setcmd.fset_command(live, "MARK_GAP ²").error
