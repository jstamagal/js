"""patch falls back to a match that reads smart quotes, Unicode dashes, no-break
spaces and trailing whitespace as plain ASCII, and writes the edit over the
file's own bytes: every line and character the edit kept stays as it was."""

from __future__ import annotations

from js.toolkit import ToolContext, fs


def _read(tmp_path, body: str, name: str = "f.txt"):
    target = tmp_path / name
    target.write_bytes(body.encode("utf-8"))
    context = ToolContext(cwd=tmp_path)
    assert not fs.read(name, context=context).startswith("ERROR")
    return target, context


def _lines(target) -> list[bytes]:
    return target.read_bytes().splitlines(keepends=True)


def test_smart_quotes_in_the_file_match_straight_quotes_in_old_string(tmp_path):
    body = "a = 1\nmsg = “hello”\nb = ‘x’\nz = 9\n"
    target, context = _read(tmp_path, body)
    before = _lines(target)

    result = fs.patch(file_path="f.txt", old_string="msg = \"hello\"\nb = 'x'",
                      new_string="msg = \"hello\"\nb = 'y'", context=context)

    assert result.startswith("patched "), result
    after = _lines(target)
    assert after[0] == before[0] and after[3] == before[3]
    # The kept line inside the match keeps its smart quotes.
    assert after[1] == before[1]
    # The changed line keeps the smart quotes the edit did not touch.
    assert after[2] == "b = ‘y’\n".encode()


def test_no_break_space_in_the_file_matches_a_plain_space(tmp_path):
    body = "keep\nprice: 100 euro\nlast\n"
    target, context = _read(tmp_path, body)

    result = fs.patch(file_path="f.txt", old_string="price: 100 euro", new_string="price: 200 euro",
                      context=context)

    assert result.startswith("patched "), result
    assert target.read_bytes() == "keep\nprice: 200 euro\nlast\n".encode()


def test_trailing_whitespace_in_the_file_is_ignored_and_kept(tmp_path):
    body = "def f():   \n    x = 1\t\n    return x  \n\nprint(f())\n"
    target, context = _read(tmp_path, body)
    before = _lines(target)

    result = fs.patch(file_path="f.txt", old_string="def f():\n    x = 1\n    return x\n",
                      new_string="def f():\n    x = 2\n    return x\n", context=context)

    assert result.startswith("patched "), result
    after = _lines(target)
    assert after[0] == before[0]
    assert after[2] == before[2]
    assert after[3:] == before[3:]
    assert after[1] == b"    x = 2\t\n"


def test_unicode_dashes_in_the_file_match_a_hyphen(tmp_path):
    body = "title — part one\nrange 1–5\nminus −1\nend\n"
    target, context = _read(tmp_path, body)
    before = _lines(target)

    result = fs.patch(file_path="f.txt", old_string="title - part one\nrange 1-5",
                      new_string="title - part two\nrange 1-5", context=context)

    assert result.startswith("patched "), result
    after = _lines(target)
    assert after[0] == "title — part two\n".encode()
    assert after[1:] == before[1:]


def test_crlf_file_keeps_its_line_endings_and_untouched_lines(tmp_path):
    body = "one  \r\nsay “hi”\r\nthree\r\n"
    target, context = _read(tmp_path, body)
    before = _lines(target)

    result = fs.patch(file_path="f.txt", old_string="one\nsay \"hi\"\nthree",
                      new_string="one\nsay \"bye\"\nthree", context=context)

    assert result.startswith("patched "), result
    after = _lines(target)
    assert after[0] == before[0] and after[2] == before[2]
    assert after[1] == "say “bye”\r\n".encode()


def test_an_exact_match_wins_over_a_fuzzy_one(tmp_path):
    body = 'a = "x"\nb = “x”\n'
    target, context = _read(tmp_path, body)

    result = fs.patch(file_path="f.txt", old_string='= "x"', new_string='= "y"', context=context)

    assert result.startswith("patched "), result
    assert target.read_bytes() == 'a = "y"\nb = “x”\n'.encode()


def test_two_fuzzy_matches_need_replace_all(tmp_path):
    body = "q(“a”)\nr\nq(“a”)\n"
    target, context = _read(tmp_path, body)

    refused = fs.patch(file_path="f.txt", old_string='q("a")', new_string='q("b")', context=context)
    assert refused.startswith("ERROR"), refused
    assert target.read_bytes() == body.encode()

    result = fs.patch(file_path="f.txt", old_string='q("a")', new_string='q("b")', replace_all=True,
                      context=context)
    assert result.startswith("patched "), result
    assert target.read_bytes() == "q(“b”)\nr\nq(“b”)\n".encode()


def test_a_fuzzy_edit_that_changes_nothing_is_refused(tmp_path):
    body = "say “hi”\n"
    target, context = _read(tmp_path, body)

    # new_string only swaps the straight quotes for the smart ones the file has.
    refused = fs.patch(file_path="f.txt", old_string='say "hi"', new_string="say “hi”",
                       context=context)

    assert refused.startswith("ERROR"), refused
    assert target.read_bytes() == body.encode()


def test_a_fuzzy_match_still_needs_its_lines_read(tmp_path):
    target = tmp_path / "f.txt"
    target.write_bytes("".join(f"line {n}\n" for n in range(1, 11)).encode() + "say “hi”\n".encode())
    context = ToolContext(cwd=tmp_path)
    fs.read("f.txt", start_line=1, end_line=5, context=context)

    refused = fs.patch(file_path="f.txt", old_string='say "hi"', new_string='say "bye"', context=context)

    assert refused.startswith("ERROR"), refused
    assert "11" in refused
    assert target.read_bytes().endswith("say “hi”\n".encode())


def test_a_batch_mixes_exact_and_fuzzy_edits_and_each_sees_the_last(tmp_path):
    body = "x = 1\ny = “old”\n"
    target, context = _read(tmp_path, body)

    result = fs.patch(file_path="f.txt", edits=[
        {"old_string": "x = 1", "new_string": "x = 2"},
        {"old_string": 'y = "old"', "new_string": 'y = "new"'},
        {"old_string": "x = 2", "new_string": "x = 3"},
    ], context=context)

    assert result.startswith("patched "), result
    assert target.read_bytes() == "x = 3\ny = “new”\n".encode()


def test_undo_restores_the_bytes_before_a_fuzzy_patch(tmp_path):
    body = "a b  \nc\n"
    target, context = _read(tmp_path, body)

    assert fs.patch(file_path="f.txt", old_string="a b\nc", new_string="a b\nd",
                    context=context).startswith("patched ")
    assert fs.undo("f.txt", context=context).startswith("restored ")

    assert target.read_bytes() == body.encode()
