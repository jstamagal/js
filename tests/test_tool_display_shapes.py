"""Which shape the screen gives each tool's call and result.

Each test renders a call and its result through `display.render_exchange`
(or the fold row) and checks the shape the renderer chose: what the header
carries, whether the program or the result is a panel, a numbered file view
or a diff, and whether a call collapses to its header. A tool the screen
knows nothing about is the baseline the shapes differ from."""

from __future__ import annotations

import re

from js import display

SGR = re.compile(r"\x1b\[[0-9;]*m")
NOTHING_QUIET = {"TOOLS_QUIET": []}
UNKNOWN = "mcp_unknown_tool"
SHELL_ENVELOPE = "shell=bash\nexit={code}\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n"
PATCHED = ("patched /tmp/a.py (1 edit, hash abc123)\n--- a/x\n+++ b/x\n"
           "@@ -4,2 +4,2 @@\n-old text\n+new text\n context")
READ_RESULT = "4|fourth row\n5|fifth row\n6|sixth row\n[40 total lines; continue with start_line=7]"
FIVE_LINES = "\n".join(f"result row {n}" for n in range(1, 6))


def _rows(name: str, args: dict | None, result, level: int, **options) -> list[str]:
    """The exchange's rows without colour, rail or blank rows."""
    text = display.render_exchange(name, args, result, level=level, width=200,
                                   settings=options.pop("settings", NOTHING_QUIET), **options)
    rows = [SGR.sub("", row).removeprefix(display.RAIL).strip() for row in text.splitlines()]
    return [row for row in rows if row]


def _header(rows: list[str]) -> str:
    return rows[0]


def _numbered(rows: list[str], number: int, text: str) -> bool:
    """A row of a numbered view: the number, then `text`."""
    return any(re.fullmatch(rf"{number}\s+{re.escape(text)}", row) for row in rows)


def _listed(header: str, arg: str) -> bool:
    """The header lists `arg` among the other arguments as `arg=value`."""
    return re.search(rf"[(,] ?{arg}=", header) is not None


# -- the baseline ----------------------------------------------------------


def test_an_unknown_tool_shows_its_path_lists_its_arguments_and_shows_its_result_as_a_panel():
    args = {"path": "/srv/data/file.txt", "mode": "fast"}
    rows = _rows(UNKNOWN, args, FIVE_LINES, level=2, preview=3)
    assert "/srv/data/file.txt" in _header(rows) and _listed(_header(rows), "mode")
    assert rows[1:4] == ["result row 1", "result row 2", "result row 3"]
    assert "result row 4" not in rows
    assert display.call_kind(UNKNOWN, args, NOTHING_QUIET) == "loud"
    assert "4|fourth row" in _rows(UNKNOWN, {}, READ_RESULT, level=3)
    assert "+new text" in _rows(UNKNOWN, {}, PATCHED, level=1)


# -- file tools ------------------------------------------------------------


def test_read_shows_its_path_its_range_apart_and_its_result_as_numbered_file_rows():
    """The range stays off the argument list below TOOLS_DISPLAY 3."""
    args = {"file_path": "/srv/data/notes.txt", "start_line": 4, "end_line": 6}
    rows = _rows("read", args, READ_RESULT, level=3)
    header = _header(_rows("read", args, READ_RESULT, level=2))
    assert "/srv/data/notes.txt" in header
    assert "4" in header and "6" in header
    assert not _listed(header, "start_line") and not _listed(header, "end_line")
    assert _numbered(rows, 5, "fifth row")
    assert not any("|" in row or "start_line=7" in row for row in rows[1:])


def test_patch_shows_its_result_as_a_numbered_diff_and_leaves_the_edit_text_off_the_header():
    """Below TOOLS_DISPLAY 3, which lists every argument whole."""
    args = {"file_path": "/srv/a.py", "old_string": "old text", "new_string": "new text"}
    for level in (1, 3):
        rows = _rows("patch", args, PATCHED, level=level)
        assert "/srv/a.py" in _header(rows)
        assert _numbered(rows, 4, "- old text") and _numbered(rows, 4, "+ new text")
        assert _numbered(rows, 5, "context")
    header = _header(_rows("patch", args, PATCHED, level=2))
    assert not _listed(header, "old_string") and not _listed(header, "new_string")
    edits = _header(_rows("patch", {"file_path": "/srv/a.py", "edits": [{}, {}, {}]}, PATCHED, level=1))
    assert "3" in edits and not _listed(edits, "edits")


def test_write_shows_the_file_it_writes_as_numbered_rows_and_its_result_on_the_header():
    args = {"file_path": "/srv/w.txt", "content": "alpha\nbeta\ngamma\n"}
    assert display.call_kind("write", args, NOTHING_QUIET) == "diff"
    brief = _rows("write", args, "wrote it\nsecond note", level=1)
    assert len(brief) == 2 and "second note" in brief[1]
    full = _rows("write", args, "wrote it", level=2)
    header = _header(full)
    assert "/srv/w.txt" in header and "3" in header and not _listed(header, "content")
    assert _numbered(full, 1, "alpha") and _numbered(full, 3, "gamma")
    one_line = _rows("write", {"file_path": "/srv/w.txt", "content": "solo"}, "wrote it", level=2)
    assert _numbered(one_line, 1, "solo")


def test_notebook_edit_collapses_its_result_to_the_header_at_level_one():
    args = {"path": "/srv/n.ipynb", "cell": 2}
    assert display.call_kind("notebook_edit", args, NOTHING_QUIET) == "diff"
    rows = _rows("notebook_edit", args, FIVE_LINES, level=1)
    assert "result row 1" not in rows and "result row 5" in rows[-1]


# -- shell and fetch -------------------------------------------------------


def test_shell_drops_its_envelope_shows_the_command_and_one_row_at_level_one():
    args = {"command": "just build", "description": "build it"}
    result = SHELL_ENVELOPE.format(code=0, out="compiled\nlinked", err="a warning")
    assert display.call_kind("shell", args, NOTHING_QUIET) == "command"
    brief = _rows("shell", args, result, level=1)
    assert "just build" in _header(brief) and not _listed(_header(brief), "description")
    assert len(brief) == 2 and "a warning" in brief[1]
    full = _rows("shell", args, result, level=3)
    assert full[1:4] == ["compiled", "linked", "a warning"]
    assert not any("shell=" in row or "stdout" in row for row in full)


def test_a_failed_shell_command_shows_its_exit_and_last_line_even_when_quiet():
    args = {"command": "ls /missing"}
    result = SHELL_ENVELOPE.format(code=2, out="", err="no such file")
    assert display.call_kind("shell", args, NOTHING_QUIET) == "quiet"
    output = display.tool_output("shell", result)
    assert output.exit_code == 2 and output.failed
    rows = _rows("shell", args, result, level=1)
    assert len(rows) == 2 and "2" in rows[1] and "no such file" in rows[1]


def test_shell_names_a_folded_call_by_its_program():
    assert display.fold_label("shell", {"command": "git status --short"}) == "git status"
    assert display.fold_label("shell", {"command": "ls -la /tmp"}) == "ls"
    assert display.fold_label("terminal_session", {"command": "ls -la /tmp"}) == "ls -la /tmp"


def test_a_program_shows_its_first_line_on_the_header_and_all_of_it_as_a_panel():
    programs = {"shell": "command", "terminal_session": "command", "kernel": "code", "toolbox": "source"}
    for name, key in programs.items():
        args = {key: "echo one\necho two\necho three"}
        assert display.call_kind(name, args, NOTHING_QUIET) == "command", name
        rows = _rows(name, args, "done", level=2)
        assert "echo one" in _header(rows) and "echo two" not in _header(rows), name
        assert rows[1:4] == ["echo one", "echo two", "echo three"], name
        one = _rows(name, {key: "echo one"}, "done", level=2)
        assert "echo one" in _header(one) and "echo one" not in one[1:], name


def test_kernel_and_toolbox_print_their_result_on_the_header_at_level_one():
    for name, key in (("kernel", "code"), ("toolbox", "source"), ("terminal_session", "command")):
        rows = _rows(name, {key: "work()"}, FIVE_LINES, level=1)
        assert len(rows) == 2 and "result row 5" in rows[1], name


def test_fetch_and_browse_show_the_url_and_fold_by_host():
    for name in ("fetch", "browse"):
        args = {"url": "https://example.org/some/page", "path": "/srv/ignored"}
        assert "https://example.org/some/page" in _header(_rows(name, args, "page text", level=1))
        assert display.fold_label(name, args) == "example.org"


# -- search ----------------------------------------------------------------


def test_a_search_shows_what_it_looks_for_on_the_header():
    searches = {
        "fs_search": ("pattern", "needle.*"), "ast_search": ("pattern", "def $F"),
        "exa_search": ("query", "the query"), "serper_search": ("query", "the query"),
        "tavily_search": ("query", "the query"), "docs_search": ("topic", "the topic"),
        "tool_discovery": ("query", "the query"),
    }
    for name, (key, value) in searches.items():
        args = {"path": "/srv/elsewhere", key: value}
        header = _header(_rows(name, args, FIVE_LINES, level=1))
        assert value in header and not _listed(header, key), name
        assert _listed(header, "path"), name
        assert display.fold_label(name, args) == value, name


# -- subagent and swarm tools -----------------------------------------------


def test_a_message_shows_what_it_said_in_place_of_the_result():
    said = {"send": ("to", "bob", "text"), "post_task": ("title", "Fix it", "body"),
            "finish_task": ("id", 7, "result")}
    for name, (key, value, arg) in said.items():
        args = {key: value, arg: "first said\nsecond said"}
        brief = _rows(name, args, "acknowledged", level=1)
        assert str(value) in _header(brief) and not _listed(_header(brief), arg), name
        assert brief[-2:] == ["first said", "second said"], name
        full = _rows(name, args, "acknowledged", level=3)
        assert not _listed(_header(full), arg), name
        assert "first said" in full and "acknowledged" in full, name


def test_a_subagent_call_shows_its_agent_and_is_an_agent_run():
    args = {"tasks": ["look around"], "agent_id": "helper"}
    assert display.call_kind("task", args, NOTHING_QUIET) == "agent"
    assert "helper" in _header(_rows("task", args, "found it", level=1))


def test_claim_and_release_show_their_key():
    for name in ("claim", "release"):
        header = _header(_rows(name, {"key": "build-lock", "path": "/srv/x"}, "ok", level=1))
        assert "build-lock" in header and _listed(header, "path"), name


# -- the rest --------------------------------------------------------------


def test_plan_shows_its_name_and_its_content_as_a_panel():
    args = {"plan_name": "rollout", "version": "1", "content": "# Steps\none\ntwo"}
    rows = _rows("plan", args, "saved", level=2)
    assert "rollout" in _header(rows) and not _listed(_header(rows), "content")
    assert rows[1:4] == ["# Steps", "one", "two"]
    assert display.call_kind("plan", args, NOTHING_QUIET) == "loud"


def test_skill_shows_its_name():
    assert "reviewing" in _header(_rows("skill", {"name": "reviewing"}, "loaded", level=1))


def test_a_quiet_call_of_any_tool_is_one_row():
    rows = _rows("read", {"file_path": "/srv/n.txt"}, READ_RESULT, level=1, settings={"TOOLS_QUIET": ["read"]})
    assert len(rows) == 1 and "/srv/n.txt" in rows[0]
