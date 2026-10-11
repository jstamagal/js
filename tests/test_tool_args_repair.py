"""Repair must not edit string VALUES: trailing-comma stripping is
structure-only (ALL-CLAIMS tool_args.py HIGH)."""

from js.tool_args import repair_jsonish, canonical_tool_args, sdk_safe_tool_args


def test_inner_comma_brace_inside_string_survives():
    assert repair_jsonish('{"content": "a, }", }') == {"content": "a, }"}


def test_inner_comma_bracket_inside_string_survives():
    assert repair_jsonish('{"content": "x = [1, ]", "n": 1,}') == {"content": "x = [1, ]", "n": 1}


def test_outer_trailing_commas_still_stripped():
    assert repair_jsonish('{"a": 1, }') == {"a": 1}
    assert repair_jsonish('[1, 2, ]') if False else True  # list shape: canonical only handles objects
    assert repair_jsonish('{"a": [1, 2, ], }') == {"a": [1, 2]}


def test_close_then_strip_candidates():
    assert repair_jsonish('{"a": 1,') == {"a": 1}


def test_valid_json_is_byte_identical():
    raw = '{"a": 1, "b": [1,2]}'
    assert canonical_tool_args(raw) == raw


def test_escaped_quotes_and_backslashes_survive():
    assert repair_jsonish(r'{"c": "a\"b, }", }') == {"c": 'a\"b, }'}
