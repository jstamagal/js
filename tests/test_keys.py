"""The keys file: named actions, their default keys, remapping and its errors,
and the table that turns a keymap into prompt_toolkit bindings."""

from __future__ import annotations

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_bindings import _parse_key

from js import keys, screen

# The keys the async screen answered to before the keys file existed, with
# Ctrl-R moved to history_search and reasoning_toggle on Ctrl-O.
DEFAULTS = {
    "submit": (("enter",),),
    "history_search": (("c-r",),),
    "ex_open": ((":",),),
    "ex_run": (("enter",),),
    "ex_cancel": (("escape",),),
    "interrupt": (("c-c",),),
    "eof": (("c-d",),),
    "suspend": (("c-z",),),
    "reasoning_toggle": (("c-o",),),
    "redraw": (("c-l",),),
    "scroll_up": (("pageup",),),
    "scroll_down": (("pagedown",),),
    "complete": (("tab",),),
}


def _parse(text: str):
    return keys.parse(text, "/k")


def test_the_defaults_are_the_screens_keys():
    assert keys.default_keymap() == DEFAULTS


def test_an_empty_or_comment_only_file_gives_the_defaults():
    keymap, errors = _parse("# my keys\n\n   \n")

    assert keymap == DEFAULTS
    assert errors == []


def test_a_missing_file_gives_the_defaults(tmp_path):
    assert keys.load(tmp_path / "keys") == (DEFAULTS, [])


def test_bind_adds_a_key_to_an_action():
    keymap, errors = _parse("bind c-f history_search\n")

    assert errors == []
    assert keymap["history_search"] == (("c-r",), ("c-f",))


def test_bind_takes_the_key_off_another_action_in_the_same_place():
    keymap, errors = _parse("bind c-r reasoning_toggle\n")

    assert errors == []
    assert keymap["reasoning_toggle"] == (("c-o",), ("c-r",))
    assert keymap["history_search"] == ()


def test_bind_leaves_an_action_that_fires_elsewhere_alone():
    # ex_cancel fires in the ex line, submit on the input line.
    keymap, _ = _parse("bind enter ex_cancel\n")

    assert keymap["submit"] == (("enter",),)
    assert keymap["ex_run"] == ()
    assert ("enter",) in keymap["ex_cancel"]


def test_a_key_alias_is_the_same_key():
    # c-m is prompt_toolkit's name for enter; redraw fires everywhere.
    keymap, _ = _parse("bind c-m redraw\n")

    assert keymap["submit"] == ()
    assert keymap["ex_run"] == ()
    assert keymap["redraw"] == (("c-l",), ("c-m",))


def test_bind_takes_a_key_sequence():
    keymap, errors = _parse("bind escape r redraw\n")

    assert errors == []
    assert ("escape", "r") in keymap["redraw"]
    assert keymap["ex_cancel"] == (("escape",),)


def test_unbind_takes_a_key_off_every_action():
    keymap, errors = _parse("unbind enter\nunbind c-z\n")

    assert errors == []
    assert keymap["submit"] == keymap["ex_run"] == keymap["suspend"] == ()


def test_a_trailing_comment_and_a_leading_slash_are_allowed():
    keymap, errors = _parse("/bind c-f history_search   # search on c-f too\n")

    assert errors == []
    assert ("c-f",) in keymap["history_search"]


def test_each_bad_line_is_one_error_naming_its_line_and_is_skipped():
    text = "\n".join([
        "bind c-f history_search",
        "bind ctrl-q redraw",
        "bind c-q no_such_action",
        "rebind c-q redraw",
        "bind redraw",
        "unbind",
        "bind c-g redraw",
    ])

    keymap, errors = _parse(text)

    assert [error.split(":")[:2] for error in errors] == [
        ["/k", "2"], ["/k", "3"], ["/k", "4"], ["/k", "5"], ["/k", "6"],
    ]
    assert all("\n" not in error for error in errors)
    assert ("c-f",) in keymap["history_search"]
    assert ("c-g",) in keymap["redraw"]
    assert not any(("c-q",) in seqs for seqs in keymap.values())


def test_key_names_are_prompt_toolkits():
    for good in ("c-r", "escape", "enter", "tab", "pageup", "f5", "space", "x", ":", "s-tab"):
        assert keys.key_error(good) is None, good
    for bad in ("ctrl-r", "C-r", "esc", "", "f99"):
        assert keys.key_error(bad) is not None, bad


def test_an_unreadable_file_is_one_error_and_the_defaults(tmp_path):
    path = tmp_path / "keys"
    path.write_bytes(b"\xff\xfe bind")

    keymap, errors = keys.load(path)

    assert keymap == DEFAULTS
    assert len(errors) == 1


def test_keys_file_setting_names_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert keys.keys_file({}) == tmp_path / ".js" / "keys"
    assert keys.keys_file({"keys": {"file": "~/elsewhere/keys"}}) == tmp_path / "elsewhere" / "keys"


def _bound(kb: KeyBindings) -> dict[str, set[tuple]]:
    table: dict[str, set[tuple]] = {}
    for binding in kb.bindings:
        table.setdefault(binding.handler.__name__, set()).add(tuple(binding.keys))
    return table


def _named(name: str):
    def handler(event) -> None:
        pass

    handler.__name__ = name
    return handler


def _expected(keymap) -> dict[str, set[tuple]]:
    return {name: {tuple(_parse_key(key) for key in seq) for seq in seqs}
            for name, seqs in keymap.items() if seqs}


def test_bind_actions_binds_every_key_of_every_action_to_its_handler():
    keymap, _ = _parse("bind c-f history_search\nunbind c-z\nbind escape r redraw\n")
    handlers = {action.name: screen.Handler(_named(action.name)) for action in keys.ACTIONS}
    kb = KeyBindings()

    screen.bind_actions(kb, keymap, handlers)

    assert _bound(kb) == _expected(keymap)


def test_bind_actions_carries_each_actions_filter_and_eager_flag():
    handlers = {"ex_cancel": screen.Handler(_named("ex_cancel"), False, eager=True)}
    kb = KeyBindings()

    screen.bind_actions(kb, {"ex_cancel": (("escape",),)}, handlers)

    [binding] = kb.bindings
    assert binding.eager()
    assert not binding.filter()


def test_the_blocking_prompt_binds_the_history_search_keys():
    keymap, _ = _parse("bind c-f history_search\n")

    kb = keys.prompt_bindings(keymap)

    assert _bound(kb) == {"history_search": _expected(keymap)["history_search"]}


def test_a_collapsed_reasoning_block_names_the_toggle_key():
    scroll = screen.Scrollback()
    scroll.toggle_key = keys.describe(("c-t",))
    block = scroll.reasoning(1)
    block.append("thinking")
    block.finish()

    assert "Ctrl-T" in block.render()
