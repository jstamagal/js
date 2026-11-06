"""Every format js ships agrees with the event that draws it: each hole it
uses is a word the event passes, and the words it draws are the entry's
own text."""

from __future__ import annotations

from string import Formatter

import pytest

from js import fset
from js import messages as msgs

_FORMATTER = Formatter()


def _sample(entry: msgs.Message) -> dict:
    """A value for every field of ``entry``, each with a space in it where
    its spec takes text, so a field that is not the last is seen to stay one
    word."""
    values: dict = {}
    for name, spec, _conversion in entry.fields():
        for _literal, nested, _spec, _conv in _FORMATTER.parse(spec):
            if nested is not None:
                values[nested] = 6
    for index, (name, spec, conversion) in enumerate(entry.fields()):
        if name in values:
            continue
        for candidate in (f"w{index} x{index}", 1234.5, 3):
            try:
                format(_FORMATTER.convert_field(candidate, conversion), _FORMATTER.vformat(spec, (), values))
            except (ValueError, TypeError):
                continue
            values[name] = candidate
            break
    return values


def _shipped_formats() -> dict[str, str]:
    """Every format `js/fset` and the built-in themes set, by `file:NAME`."""
    found = {f"fset:{name}": value for name, value in fset.read_file(fset.PACKAGE_FSET).items()
             if name in msgs.ENTRIES}
    for theme in fset.themes():
        found.update({f"{theme}:{name}": value for name, value in fset.theme_entries(theme).items()
                      if name in msgs.ENTRIES})
    return found


def test_every_shipped_format_uses_only_words_its_event_passes():
    refused = {}
    for where, value in _shipped_formats().items():
        entry = msgs.ENTRIES[where.split(":", 1)[1]]
        error = fset.format_error(value, len(entry.fields()))
        if error is not None:
            refused[where] = str(error)
    assert refused == {}


def test_every_entry_s_default_format_uses_only_words_its_event_passes():
    refused = {name: str(error) for name, entry in msgs.ENTRIES.items()
               if (error := fset.format_error(entry.default_format(), len(entry.fields()))) is not None}
    assert refused == {}


def test_every_shipped_fset_format_draws_its_entry_s_own_words():
    """A js/fset format adds colour to its entry's text and changes no word."""
    changed = {}
    for name in fset.read_file(fset.PACKAGE_FSET):
        entry = msgs.ENTRIES.get(name)
        if entry is None:
            continue
        values = _sample(entry)
        drawn = fset.to_plain(fset.runs(fset.defaults()[name], fset.words(msgs._holes(entry, values))))
        expected = msgs.banner(entry.text(**values)) if entry.banner else entry.text(**values)
        if drawn != expected.rstrip(" "):
            changed[name] = (drawn, expected)
    assert changed == {}


@pytest.mark.parametrize("name", sorted(msgs.ENTRIES))
def test_an_entry_drawn_through_its_default_format_reads_as_its_text(name):
    entry = msgs.ENTRIES[name]
    values = _sample(entry)
    with fset.held():
        drawn = fset.to_plain(fset.runs(entry.default_format(), fset.words(msgs._holes(entry, values))))
    expected = msgs.banner(entry.text(**values)) if entry.banner else entry.text(**values)
    assert drawn == expected.rstrip(" ")
