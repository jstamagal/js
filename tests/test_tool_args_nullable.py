import pytest

from js import tool_args


@pytest.mark.parametrize("kind", ["integer", "number", "boolean", "object", "array"])
@pytest.mark.parametrize("value", ["null", " null ", None])
def test_nullable_nonstring_types_accept_stringified_null(kind, value):
    schema = {"type": [kind, "null"]}
    assert tool_args.coerce_to_schema(value, schema) is None


@pytest.mark.parametrize("schema", [
    {"type": "string"}, {"type": ["string", "null"]},
    {"type": ["integer", "string", "null"]},
    {"anyOf": [{"type": "string", "enum": ["null"]}, {"type": "null"}]},
])
def test_string_schemas_preserve_literal_null(schema):
    assert tool_args.coerce_to_schema("null", schema) == "null"


@pytest.mark.parametrize("kind", ["integer", "number", "boolean", "object", "array"])
def test_nonnullable_types_do_not_coerce_null(kind):
    schema = {"type": kind}
    assert tool_args.coerce_to_schema("null", schema) == "null"
    assert tool_args.schema_error("null", schema) is not None


def test_nullable_nested_arguments_and_union_branches():
    schema = {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": ["integer", "null"]}},
        "flag": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
    }}
    coerced = tool_args.coerce_to_schema({"items": ["1", "null", None], "flag": "null"}, schema)
    assert coerced == {"items": [1, None, None], "flag": None}
    assert tool_args.schema_error(coerced, schema) is None


def test_strict_mode_preserves_stringified_null(monkeypatch):
    monkeypatch.setattr(tool_args._settings, "live_value", lambda key: True)
    assert tool_args.coerce_to_schema("null", {"type": ["integer", "null"]}) == "null"
