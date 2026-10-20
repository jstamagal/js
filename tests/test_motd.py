"""The message of the day: which file or entry is picked, and how its bytes read."""

from __future__ import annotations

import random

from js import motd


def test_motd_file_entries_split_on_percent_lines(tmp_path):
    path = tmp_path / "motd"
    path.write_text("one\n%\ntwo\nlines\n%\n", encoding="utf-8")
    picked = {motd.pick(str(path), None, random.Random(seed)) for seed in range(20)}
    assert picked == {"one\n", "two\nlines\n"}


def test_cp437_art_decodes_and_its_sauce_record_is_dropped():
    art = b"\x1b[1;31m\xdb\xdb\x1b[0m\r\n" + b"\x1aSAUCE00" + b" " * 121
    assert motd.decode(art) == "\x1b[1;31m██\x1b[0m\r\n"


def test_motd_file_wins_over_motd_dir_and_unset_is_none(tmp_path):
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "a.ans").write_text("from dir", encoding="utf-8")
    (tmp_path / "file").write_text("from file", encoding="utf-8")
    assert motd.pick(str(tmp_path / "file"), str(tmp_path / "dir")) == "from file"
    assert motd.pick(None, str(tmp_path / "dir")) == "from dir"
    assert motd.pick(None, None) is None


def test_a_form_feed_in_an_entry_does_not_split_it():
    assert motd.entries("art\x0c%\nmore\n%\nnext\n") == ["art\x0c%\nmore\n", "next\n"]


def test_the_motd_leads_the_startup_banner_the_repl_prints(tmp_path):
    from types import SimpleNamespace

    from js import cli

    (tmp_path / "motd").write_text("\x1b[31mHELLO\n", encoding="utf-8")
    cfg = SimpleNamespace(agent_id="a", prompts_dir=tmp_path, session_file=tmp_path / "s.jsonl")
    state = {"settings": {"MOTD_FILE": str(tmp_path / "motd")}, "model": "m"}
    assert cli._startup_banner(cfg, state, [], []).startswith("\x1b[31mHELLO" + motd.RESET + "\n")


def test_a_motd_line_starts_in_the_colours_the_line_before_left():
    assert motd.lines_for_scrollback("\x1b[31mTOP\nBOTTOM\x1b[0m\nPLAIN") == (
        "\x1b[31mTOP\n\x1b[31mBOTTOM\x1b[0m\nPLAIN")


def test_motd_line_ends_are_normalized():
    assert motd.lines_for_scrollback("a\r\nb\rc") == "a\nb\nc"


def test_the_carried_colour_does_not_grow_with_the_art():
    art = "\n".join(f"\x1b[3{n % 8}mrow{n}" for n in range(2000))
    out = motd.lines_for_scrollback(art)
    assert len(out) < 2 * len(art)
    assert out.split("\n")[1] == "\x1b[30m\x1b[31mrow1"


def test_a_reset_and_an_attribute_off_clear_what_is_carried():
    assert motd.lines_for_scrollback("\x1b[1;31ma\x1b[22mb\nc\x1b[0m\nd") == "\x1b[1;31ma\x1b[22mb\n\x1b[31mc\x1b[0m\nd"
