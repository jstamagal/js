"""Repair must not edit string VALUES: trailing-comma stripping is
structure-only (ALL-CLAIMS tool_args.py HIGH)."""

from js.tool_args import repair_jsonish, canonical_tool_args, sdk_safe_tool_args


def test_inner_comma_brace_inside_string_survives():
    assert repair_jsonish('{"content": "a, }", }') == {"content": "a, }"}


def test_inner_comma_bracket_inside_string_survives():
    assert repair_jsonish('{"content": "x = [1, ]", "n": 1,}') == {"content": "x = [1, ]", "n": 1}


def test_outer_trailing_commas_still_stripped():
    assert repair_jsonish('{"a": 1, }') == {"a": 1}
    assert repair_jsonish('{"a": [1, 2, ], }') == {"a": [1, 2]}


def test_close_then_strip_candidates():
    assert repair_jsonish('{"a": 1,') == {"a": 1}


def test_valid_json_is_byte_identical():
    raw = '{"a": 1, "b": [1,2]}'
    assert canonical_tool_args(raw) == raw


def test_escaped_quotes_and_backslashes_survive():
    assert repair_jsonish(r'{"c": "a\"b, }", }') == {"c": 'a\"b, }'}


def test_anyof_object_branch_coerces_stringified_object():
    """fetch's headers is anyOf[object, array] (ALL-CLAIMS tool_args MED)."""
    from js.tool_args import coerce_to_schema

    schema = {"anyOf": [{"type": "object"}, {"type": "array"}]}
    assert coerce_to_schema('{"A": "b"}', schema) == {"A": "b"}


def test_anyof_array_branch_coerces_stringified_array():
    from js.tool_args import coerce_to_schema

    schema = {"anyOf": [{"type": "array"}, {"type": "null"}]}
    assert coerce_to_schema("[1, 2]", schema) == [1, 2]


def test_anyof_with_string_branch_keeps_the_string():
    from js.tool_args import coerce_to_schema

    schema = {"anyOf": [{"type": "string"}, {"type": "object"}]}
    assert coerce_to_schema('{"A": "b"}', schema) == '{"A": "b"}'


def test_oneof_scalar_union_and_non_json_unchanged():
    from js.tool_args import coerce_to_schema

    assert coerce_to_schema("15", {"oneOf": [{"type": "integer"}, {"type": "object"}]}) == 15
    assert coerce_to_schema("not json", {"anyOf": [{"type": "object"}, {"type": "array"}]}) == "not json"
    assert coerce_to_schema("x", {"$ref": "#/x"}) == "x"


def test_recursion_depth_spam_is_safe():
    """A model spamming brackets must not kill the stream handler
    (ALL-CLAIMS tool_args MED: RecursionError escaped every guard)."""
    from js.tool_args import is_json_object, canonical_tool_args

    raw = "[" * 200000
    assert is_json_object(raw) is False
    assert canonical_tool_args(raw) == raw
    assert sdk_safe_tool_args(raw) == "{}"
    raw_dict = "{" * 200000
    assert sdk_safe_tool_args(raw_dict) == "{}"


def test_coerce_does_not_mutate_the_schema():
    """The declared type list must survive repeated calls (shinda #824)."""
    import copy

    from js.tool_args import coerce_to_schema

    schema = {"type": ["object"], "anyOf": [{"type": "array"}]}
    snapshot = copy.deepcopy(schema)
    coerce_to_schema('{"A": "b"}', schema)
    coerce_to_schema('{"A": "b"}', schema)
    assert schema == snapshot


def test_nested_anyof_string_branch_keeps_the_string():
    """A string valid under a NESTED anyOf branch is not coerced (shinda #871)."""
    from js.tool_args import coerce_to_schema

    schema = {"anyOf": [{"anyOf": [{"type": "string"}, {"type": "object"}]}, {"type": "array"}]}
    assert coerce_to_schema("[1]", schema) == "[1]"
    assert coerce_to_schema('{"a": 1}', schema) == '{"a": 1}'  # string branch: string is valid


def test_nested_union_coerces_object_properties():
    from js.tool_args import coerce_to_schema, schema_error

    schema = {"anyOf": [
        {"type": "null"},
        {"oneOf": [
            {"type": "object", "properties": {"count": {"type": "integer"}},
             "required": ["count"], "additionalProperties": False},
            {"type": "array", "items": {"type": "boolean"}},
        ]},
    ]}

    actual = coerce_to_schema('{"count": "3"}', schema)

    assert actual == {"count": 3}
    assert schema_error(actual, schema) is None


def test_registered_strict_mode_disables_repair_and_coercion():
    import pytest
    from js import settings
    from js.tool_args import coerce_to_schema

    settings.bind_live_settings({"TOOL_ARGUMENT_STRICT": True})
    with pytest.raises(ValueError):
        repair_jsonish('{"count": 1,}')
    assert coerce_to_schema({"count": "1"}, {"type": "object", "properties": {"count": {"type": "integer"}}}) == {"count": "1"}
