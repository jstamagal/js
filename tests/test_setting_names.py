"""Every setting name follows the naming rules in AGENTS.md (js-y3c.1)."""

from __future__ import annotations

import re

from js import settings

# Display settings keep dotted names until /fset takes them over.
_NAMES = [spec.key for spec in settings.REGISTRY if "." not in spec.key]

# Clipped words a name must spell out.
_CLIPPED = {"CTX", "CFG", "CONF", "MSG", "MSGS", "SECS", "SEC", "TMO", "BUF", "NUM", "CNT", "LEN", "DFLT", "PREF"}


def test_a_name_is_upper_snake():
    assert [name for name in _NAMES if not re.fullmatch(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)*", name)] == []


def test_a_name_spells_its_words_out():
    assert [name for name in _NAMES if _CLIPPED & set(name.split("_"))] == []


def test_a_suffix_says_what_the_value_is():
    """_FILE and _DIR name a path; _MAX is a cap, _TIMEOUT and _WAIT are seconds."""
    kinds = {"_FILE": {"str"}, "_DIR": {"str"}, "_MAX": {"int", "float"},
             "_TIMEOUT": {"int", "float"}, "_WAIT": {"int", "float"}}
    wrong = [spec.key for spec in settings.REGISTRY if "." not in spec.key
             for suffix, types in kinds.items() if spec.key.endswith(suffix) and spec.type not in types]
    assert wrong == []


def test_setting_name_is_env_js_name():
    assert [name for name in _NAMES if settings.canonical_env_name(name) != f"JS_{name}"] == []
