"""The skill picker: `/skills` in the REPL.

`SkillPicker` is the state and the keys; `application()` puts it on the
screen with prompt_toolkit, as the session picker does. It lists every
discovered skill with its source and description, each switched on or off.
Enter returns the names switched off, the value of the SKILLS_DISABLED
setting; closing returns None.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input, create_input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.styles import Style

from . import fset
from . import messages as msgs
from .skills import SkillMetadata

_PAGE = 10
_MOVES = {"up": -1, "k": -1, "down": 1, "j": 1, "pageup": -_PAGE, "pagedown": _PAGE}
_FIRST = ("home", "g")
_LAST = ("end", "G")


class _Closed:
    pass


CLOSED = _Closed()


def _style() -> Style:
    """The picker in /fset's colours."""
    c = {role: fset.hex_for(role) or "" for role in ("COLOR_ACCENT", "COLOR_OK", "COLOR_DIM")}
    return Style.from_dict(
        {
            "title": f"{c['COLOR_ACCENT']} bold",
            "keys": c["COLOR_OK"],
            "query": "bold",
            "columns": c["COLOR_DIM"],
            "selected": "reverse",
            "off": c["COLOR_DIM"],
        }
    )


class SkillPicker:
    """The picker's state. `press(key)` drives it: a key name as prompt_toolkit
    spells it (`up`, `enter`, `escape`, `backspace`, `c-u`) or a character.

    ``disabled`` is the SKILLS_DISABLED list. A name in it that names no listed
    skill (a project skill of another directory) stays in the result."""

    def __init__(self, skills: Sequence[SkillMetadata], disabled: Iterable[str] = ()) -> None:
        self.skills = sorted(skills, key=lambda skill: skill.name.casefold())
        wanted = {name.casefold() for name in disabled}
        listed = {skill.name.casefold() for skill in self.skills}
        self.off = {skill.name.casefold() for skill in self.skills if skill.name.casefold() in wanted}
        self._unlisted = tuple(name for name in disabled if name.casefold() not in listed)
        self.screen = "list"
        self.query_text = ""
        self.shown: list[SkillMetadata] = list(self.skills)
        self.cursor = 0

    # -- state -------------------------------------------------------------------

    def refresh(self) -> None:
        """Filter the list by the query: every word in the name, source or description."""
        terms = [term.casefold() for term in self.query_text.split()]
        self.shown = [
            skill for skill in self.skills
            if all(term in f"{skill.name} {skill.source} {skill.description}".casefold() for term in terms)
        ]
        self.cursor = min(self.cursor, max(0, len(self.shown) - 1))

    def current(self) -> SkillMetadata | None:
        return self.shown[self.cursor] if 0 <= self.cursor < len(self.shown) else None

    def move(self, delta: int) -> None:
        if self.shown:
            self.cursor = max(0, min(len(self.shown) - 1, self.cursor + delta))

    def toggle(self) -> None:
        skill = self.current()
        if skill is not None:
            self.off ^= {skill.name.casefold()}

    def disabled(self) -> tuple[str, ...]:
        """The SKILLS_DISABLED value: the listed skills switched off, by name,
        then the names the list does not hold."""
        listed = tuple(skill.name for skill in self.skills if skill.name.casefold() in self.off)
        return listed + self._unlisted

    # -- keys --------------------------------------------------------------------

    def press(self, key: str) -> tuple[str, ...] | _Closed | None:
        """Apply one key. A tuple (the chosen SKILLS_DISABLED) or CLOSED ends
        the picker; None keeps it open."""
        if key == "c-c":
            return CLOSED
        if self.screen == "search":
            return self._search_key(key)
        if key in _MOVES:
            self.move(_MOVES[key])
        elif key in _FIRST or key in _LAST:
            self.cursor = max(0, len(self.shown) - 1) if key in _LAST else 0
        elif key == " ":
            self.toggle()
        elif key == "enter":
            return self.disabled()
        elif key == "/":
            self.screen = "search"
        elif key == "escape":
            if not self.query_text:
                return CLOSED
            self.query_text = ""
            self.refresh()
        elif key == "q":
            return CLOSED
        return None

    def _search_key(self, key: str) -> None:
        text = self.query_text
        if key in ("up", "down", "pageup", "pagedown"):
            self.move(_MOVES[key])
            return None
        if key == "enter":
            self.screen = "list"
            return None
        if key == "escape":
            self.query_text = ""
            self.screen = "list"
        elif key == "backspace":
            self.query_text = text[:-1]
        elif key == "c-u":
            self.query_text = ""
        elif len(key) == 1 and key.isprintable():
            self.query_text = text + key
        if self.query_text != text:
            self.refresh()
        return None

    # -- view --------------------------------------------------------------------

    def title(self) -> StyleAndTextTuples:
        on = sum(1 for skill in self.skills if skill.name.casefold() not in self.off)
        text = msgs.SKILLS_TITLE.words(on=on, total=len(self.skills))
        keys = (msgs.SKILLS_QUERY_KEYS if self.screen == "search" else msgs.SKILLS_KEYS).words()
        return [("class:title", text), ("", "    "), ("class:keys", keys)]

    def subtitle(self) -> StyleAndTextTuples:
        if self.screen != "search" and not self.query_text:
            return []
        cursor = "▏" if self.screen == "search" else ""
        return [("class:query", f" / {self.query_text}{cursor}")]

    def subtitle_height(self) -> int:
        return 1 if self.screen == "search" or self.query_text else 0

    def body(self) -> tuple[StyleAndTextTuples, int]:
        """The body's fragments and the line the cursor is on."""
        if not self.shown:
            return [("class:columns", f"  {msgs.SKILLS_NONE.words()}")], 0
        source_width = max(len(skill.source) for skill in self.shown)
        # The marker takes six columns; two separate the name and source.
        name_space = max(1, get_app().output.get_size().columns - source_width - 8)
        name_width = min(max(len(skill.name) for skill in self.shown), name_space)
        out: StyleAndTextTuples = []
        for index, skill in enumerate(self.shown):
            selected = index == self.cursor
            off = skill.name.casefold() in self.off
            mark = msgs.SKILLS_OFF if off else msgs.SKILLS_ON
            name = skill.name if len(skill.name) <= name_width else skill.name[:name_width - 1] + "…"
            text = (f"{'>' if selected else ' '} {mark.words()} {name:<{name_width}}  "
                    f"{skill.source:<{source_width}}  {skill.description}")
            style = "class:selected" if selected else "class:off" if off else ""
            out += [(style, text), ("", "\n")]
        return out, self.cursor

    def application(self, *, input: Input | None = None, output: Output | None = None
                    ) -> Application[tuple[str, ...] | None]:
        kb = KeyBindings()
        names = {Keys.ControlM: "enter", Keys.ControlJ: "enter", Keys.ControlH: "backspace",
                 Keys.Backspace: "backspace", Keys.Escape: "escape", Keys.ControlC: "c-c",
                 Keys.ControlU: "c-u", Keys.Up: "up", Keys.Down: "down",
                 Keys.PageUp: "pageup", Keys.PageDown: "pagedown", Keys.Home: "home", Keys.End: "end"}

        def dispatch(event: KeyPressEvent) -> None:
            pressed = event.key_sequence[0].key
            key = names.get(pressed, pressed if isinstance(pressed, str) and len(pressed) == 1 else None)
            if key is None:
                key = event.data if len(event.data) == 1 and event.data.isprintable() else ""
            if not key:
                return
            result = self.press(key)
            if isinstance(result, tuple):
                event.app.exit(result=result)
            elif result is CLOSED:
                event.app.exit(result=None)

        kb.add(Keys.Any)(dispatch)
        kb.add("escape", eager=True)(dispatch)

        body = FormattedTextControl(
            lambda: self.body()[0],
            get_cursor_position=lambda: Point(0, self.body()[1]),
            show_cursor=False,
        )
        root = HSplit(
            [
                Window(FormattedTextControl(self.title), height=1),
                Window(FormattedTextControl(self.subtitle), height=lambda: Dimension.exact(self.subtitle_height())),
                Window(body, wrap_lines=False, always_hide_cursor=True),
            ]
        )
        return Application(layout=Layout(root), key_bindings=kb, style=_style(), full_screen=True,
                           input=input, output=output)


def pick_skills(cwd: str | Path, disabled: Iterable[str] = (), *, input=None, output=None, picker_factory=None) -> tuple[str, ...] | None:
    """Run the picker over every skill discovered from ``cwd`` and return the
    new SKILLS_DISABLED value, or None when closed."""
    from . import picker, skills

    factory = SkillPicker if picker_factory is None else picker_factory
    state = factory(skills.discover_skills(Path(cwd)).skills, disabled)
    app = state.application(input=create_input(sys.__stdin__) if input is None else input,
                            output=create_output(sys.__stdout__) if output is None else output)
    return picker.run_modal(app)
