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
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input, create_input
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.styles import Style

from . import fset, keys, messages as msgs, picker, skills
from .skills import SkillMetadata

_PAGE = picker.PAGE
_MOVES = picker.MOVES
SKILL_KEYMAPS: dict[str, keys.Keymap] = {
    "list": {"interrupt": (("c-c",),), "up": (("up",), ("k",)), "down": (("down",), ("j",)),
             "page_up": (("pageup",),), "page_down": (("pagedown",),),
             "first": (("home",), ("g",)), "last": (("end",), ("G",)),
             "toggle": ((" ",),), "choose": (("enter",),), "search": (("/",),),
             "back": (("escape",),), "quit": (("q",),)},
    "search": {"interrupt": (("c-c",),), "up": (("up",),), "down": (("down",),),
               "page_up": (("pageup",),), "page_down": (("pagedown",),),
               "choose": (("enter",),), "back": (("escape",),),
               "backspace": (("backspace",),), "clear": (("c-u",),)},
}


_Closed = picker._Closed
CLOSED = picker.CLOSED


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

    def __init__(self, skills: Sequence[SkillMetadata], disabled: Iterable[str] = (), *,
                 keymaps: dict[str, keys.Keymap] | None = None) -> None:
        self.keymaps = SKILL_KEYMAPS if keymaps is None else keymaps
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
        action = keys.action_for(self.keymaps[self.screen], key)
        if action == "interrupt":
            return CLOSED
        if self.screen == "search":
            return self._search_key(key)
        if action in _MOVES:
            self.move(_MOVES[action])
        elif action in ("first", "last"):
            self.cursor = max(0, len(self.shown) - 1) if action == "last" else 0
        elif action == "toggle":
            self.toggle()
        elif action == "choose":
            return self.disabled()
        elif action == "search":
            self.screen = "search"
        elif action == "back":
            if not self.query_text:
                return CLOSED
            self.query_text = ""
            self.refresh()
        elif action == "quit":
            return CLOSED
        return None

    def _search_key(self, key: str) -> None:
        picker.search_key(self, key)

    # -- view --------------------------------------------------------------------

    def title(self) -> StyleAndTextTuples:
        on = sum(1 for skill in self.skills if skill.name.casefold() not in self.off)
        text = msgs.SKILLS_TITLE.words(on=on, total=len(self.skills))
        keys = (msgs.SKILLS_QUERY_KEYS if self.screen == "search" else msgs.SKILLS_KEYS).with_keymap(self.keymaps[self.screen], SKILL_KEYMAPS[self.screen]).words()
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
        return picker.list_application(self, _style(), input=input, output=output)


def pick_skills(cwd: str | Path, disabled: Iterable[str] = (), *, input=None, output=None, picker_factory=None) -> tuple[str, ...] | None:
    """Run the picker over every skill discovered from ``cwd`` and return the
    new SKILLS_DISABLED value, or None when closed."""

    factory = SkillPicker if picker_factory is None else picker_factory
    state = factory(skills.discover_skills(Path(cwd)).skills, disabled)
    app = state.application(input=create_input(sys.__stdin__) if input is None else input,
                            output=create_output(sys.__stdout__) if output is None else output)
    return picker.run_modal(app)
