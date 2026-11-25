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


def test_the_carried_colour_does_not_grow_with_the_art():
    art = "\n".join(f"\x1b[3{n % 8}mrow{n}" for n in range(2000))
    out = motd.lines_for_scrollback(art)
    assert len(out) < 2 * len(art)
    assert out.split("\n")[1] == "\x1b[30m\x1b[31mrow1"
