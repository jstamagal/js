"""The login screens' curses widgets: a searchable picker, a menu, a multiselect and a one-line prompt, on the terminal's own colours, with their keymaps."""

from __future__ import annotations

import contextlib
import curses
from collections.abc import Callable, Mapping

from . import messages as msgs


def _use_terminal_colors() -> None:
    """Keep the terminal's own background instead of painting curses black.

    ``curses.wrapper`` calls ``start_color()``, and that binds color pair 0 to
    white-on-black — so every cleared line gets filled with black no matter what
    theme the terminal is running. ``use_default_colors()`` remaps the default
    fg/bg back to "whatever the terminal already had", which is what makes the
    menu sit in the surrounding colors rather than on a black rectangle.
    """
    with contextlib.suppress(curses.error):
        curses.use_default_colors()


CursesKeymap = Mapping[str, tuple[int, ...]]


_CURSES_TEXT_KEYS: CursesKeymap = {
    "choose": (10, 13, curses.KEY_ENTER), "back": (27, 3),
    "backspace": (curses.KEY_BACKSPACE, 127, 8),
}


LOGIN_KEYMAPS: dict[str, CursesKeymap] = {
    "list": {"search": (ord("/"),), "up": (curses.KEY_UP, ord("k")),
             "down": (curses.KEY_DOWN, ord("j")), "page_up": (curses.KEY_PPAGE,),
             "page_down": (curses.KEY_NPAGE,), "first": (curses.KEY_HOME,), "last": (curses.KEY_END,),
             "toggle": (ord(" "),), "edit": (ord("e"),), "all": (ord("a"), ord("A")),
             "none": (ord("n"), ord("N")), "choose": (10, 13, curses.KEY_ENTER),
             "quit": (ord("q"), 27, 3)},
    "search": {"choose": (10, 13, curses.KEY_ENTER), "back": (27,), "quit": (3,),
               "backspace": (curses.KEY_BACKSPACE, 127, 8)},
}


def _curses_action(keymap: CursesKeymap, key: int) -> str | None:
    return next((action for action, values in keymap.items() if key in values), None)


def _curses_hint(message: msgs.Message, keymap: CursesKeymap, defaults: CursesKeymap) -> msgs.Message:
    names = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_PPAGE: "pageup",
             curses.KEY_NPAGE: "pagedown", curses.KEY_ENTER: "enter", 10: "enter", 13: "enter",
             27: "escape", 3: "c-c", ord(" "): "space"}

    def convert(table):
        return {action: tuple((names.get(key, chr(key)),) for key in values)
                for action, values in table.items()}

    return message.with_keymap(convert(keymap), convert(defaults))


def _curses_picker(
    stdscr: curses.window,
    rows: list[tuple[str, str]],
    title: str,
    *,
    preselected: set[int] | None = None,
    details: list[list[str]] | None = None,
    on_edit: Callable[[curses.window, str], str | None] | None = None,
    keymaps: Mapping[str, CursesKeymap] | None = None,
    typeahead: bool = False,
    on_action: Callable[[str, int], bool] | None = None,
) -> list[int] | None:
    """Searchable chooser/checklist; selections always use original row indices.

    / edits the filter, enter returns to navigation, escape clears an in-progress
    search. Checklist all/none act on matching rows, preserving hidden choices.
    With ``on_edit``, e calls it with the row's label and shows the
    annotation it returns on the row.
    """
    keymaps = LOGIN_KEYMAPS if keymaps is None else keymaps
    rows = list(rows)
    _use_terminal_colors()
    curses.curs_set(0)
    stdscr.keypad(True)
    multiple = preselected is not None
    selected = set(preselected or ())
    idx = 0
    query = ""
    searching = False
    while True:
        matches = [i for i, row in enumerate(rows)
                   if (any(word.startswith(query.casefold()) for word in " ".join(row).casefold().split())
                       if typeahead else query.casefold() in " ".join(row).casefold())]
        idx = max(0, min(idx, len(matches) - 1))
        stdscr.clear()
        h, w = stdscr.getmaxyx()

        def draw(y: int, text: str) -> None:
            if 0 <= y < h:
                with contextlib.suppress(curses.error):
                    stdscr.addstr(y, 0, text[:max(0, w - 1)])

        draw(0, title)
        draw(1, f"/{query}" if query or searching else "-" * min(w - 1, len(title)))
        detail = details[matches[idx]] if details and matches else []
        detail = detail[:max(0, h - 7)]
        page = max(1, h - 4 - len(detail) - int(multiple))
        start = max(0, idx - page + 1)
        for off, original in enumerate(matches[start:start + page]):
            label, annotation = rows[original]
            cursor = msgs.LOGIN_PICKER_CURSOR.format() if start + off == idx else " "
            box = (msgs.LOGIN_PICKER_SELECTED.format() if original in selected else msgs.LOGIN_PICKER_UNSELECTED.format()) if multiple else ""
            tag = f"  {annotation}" if annotation else ""
            draw(off + 2, msgs.LOGIN_PICKER_ROW.format(cursor=cursor, box=box, label=label, tag=tag))
        if not matches:
            draw(2, msgs.PICKER_NO_MATCHES.text())
        for off, line in enumerate(detail):
            draw(h - 2 - len(detail) + off, line)
        if multiple:
            draw(h - 2, msgs.PICKER_SELECTED.text(selected=len(selected), total=len(rows)))
        help_text = msgs.PICKER_KEYS_CHECKLIST if multiple else msgs.PICKER_KEYS
        if multiple and on_edit is not None:
            help_text = msgs.PICKER_KEYS_CHECKLIST_EDIT
        context = "search" if searching else "list"
        hint = msgs.PICKER_KEYS_SEARCH if searching else help_text
        draw(h - 1, _curses_hint(hint, keymaps[context], LOGIN_KEYMAPS[context]).text())
        if on_action is not None and not searching:
            draw(h - 1, msgs.LOGIN_PROVIDER_KEYS.text())
        elif typeahead and not searching:
            draw(h - 1, msgs.LOGIN_TYPEAHEAD_KEYS.text())
        stdscr.refresh()
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return None
        action = _curses_action(keymaps[context], key)
        if typeahead and not searching:
            if key in (curses.KEY_BACKSPACE, 127, 8):
                query = query[:-1]
                idx = 0
                continue
            if 32 <= key < 256 and chr(key).isprintable() and key != ord("/"):
                query += chr(key)
                idx = 0
                continue
        if on_action is not None and key in (ord("r"), ord("d")) and not searching and matches:
            if on_action(chr(key), matches[idx]):
                return [matches[idx]]
            continue
        if searching:
            if action == "choose":
                searching = False
            elif action == "back":
                query = ""
                searching = False
            elif action == "quit":
                return None
            elif action == "backspace":
                query = query[:-1]
            elif 32 <= key < 256 and chr(key).isprintable():
                query += chr(key)
            idx = 0
            continue
        if action == "search":
            searching = True
            query = ""
        elif action == "up":
            idx = max(0, idx - 1)
        elif action == "down":
            idx = min(len(matches) - 1, idx + 1)
        elif action == "page_up":
            idx = max(0, idx - page)
        elif action == "page_down":
            idx = min(len(matches) - 1, idx + page)
        elif action == "first":
            idx = 0
        elif action == "last":
            idx = len(matches) - 1
        elif multiple and action == "toggle" and matches:
            original = matches[idx]
            selected.discard(original) if original in selected else selected.add(original)
        elif on_edit is not None and action == "edit" and matches:
            original = matches[idx]
            annotation = on_edit(stdscr, rows[original][0])
            if annotation is not None:
                rows[original] = (rows[original][0], annotation)
        elif multiple and action == "all":
            selected.update(matches)
        elif multiple and action == "none":
            selected.difference_update(matches)
        elif action == "choose":
            if multiple:
                return sorted(selected)
            if matches:
                return [matches[idx]]
        elif action == "quit":
            return None


def menu(
    stdscr: curses.window, items: list[str], title: str, *, details: list[list[str]] | None = None,
    typeahead: bool = False, on_action: Callable[[str, int], bool] | None = None,
) -> int | None:
    chosen = _curses_picker(stdscr, [(item, "") for item in items], title, details=details,
                            typeahead=typeahead, on_action=on_action)
    return chosen[0] if chosen else None


def multiselect(
    stdscr: curses.window,
    rows: list[tuple[str, str]],
    title: str,
    *,
    preselected: set[int],
    on_edit: Callable[[curses.window, str], str | None] | None = None,
    keymaps: Mapping[str, CursesKeymap] | None = None,
) -> list[int] | None:
    """Spacebar checklist with / filtering; None means cancel."""
    if not rows:
        return []
    return _curses_picker(stdscr, rows, title, preselected=preselected, on_edit=on_edit, keymaps=keymaps)


def line_prompt(stdscr: curses.window, prompt: str, default: str, *,
                   keymap: CursesKeymap | None = None) -> str | None:
    """A line of text typed on the bottom row, starting from ``default``.
    Enter returns it, escape returns None."""
    keymap = _CURSES_TEXT_KEYS if keymap is None else keymap
    h, w = stdscr.getmaxyx()
    text = default
    curses.curs_set(1)
    try:
        while True:
            line = f"{prompt}{text}"[-max(1, w - 1):]
            with contextlib.suppress(curses.error):
                stdscr.move(h - 1, 0)
                stdscr.clrtoeol()
                stdscr.addstr(h - 1, 0, line)
            stdscr.refresh()
            key = stdscr.getch()
            action = _curses_action(keymap, key)
            if action == "choose":
                return text
            if action == "back":
                return None
            if action == "backspace":
                text = text[:-1]
            elif 32 <= key < 256 and chr(key).isprintable():
                text += chr(key)
    finally:
        curses.curs_set(0)
