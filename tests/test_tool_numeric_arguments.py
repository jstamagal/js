"""Numeric tool arguments cannot send nonfinite values to handlers."""

import json

import pytest

from js.toolkit.core import Tool, ToolContext, call_tool, call_tool_async, coerce_value


@pytest.mark.parametrize("schema_type", ["integer", "number"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_numeric_coercion_rejects(value, schema_type):
    with pytest.raises(ValueError):
        coerce_value(value, schema_type)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_numeric_string_must_convert_to_finite_number(value):
    with pytest.raises(ValueError):
        coerce_value(value, "number")


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e999"])
@pytest.mark.parametrize("schema_type", ["integer", "number"])
def test_invalid_json_number_never_reaches_handler(tmp_path, raw, schema_type):
    calls = []
    tool = Tool("numeric", "test numeric", lambda value: calls.append(value), {"value": {"type": schema_type}})

    result = call_tool(tool, json.loads('{"value":' + raw + '}'), ToolContext(cwd=tmp_path))

    assert result.startswith("ERROR:")
    assert not calls


@pytest.mark.parametrize("value, schema_type, expected", [
    (2.0, "integer", 2), ("2", "integer", 2), (10**1000, "integer", 10**1000),
    (2.5, "number", 2.5), ("2.5", "number", 2.5), (10**1000, "number", 10**1000),
    ("NaN", "string", "NaN"), ("Infinity", "string", "Infinity"),
    (None, "integer", None), (True, "integer", True),
])
def test_valid_numeric_and_other_coercions_unchanged(value, schema_type, expected):
    assert coerce_value(value, schema_type) == expected


@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt, SystemExit])
def test_handler_failure_is_not_an_argument_error(tmp_path, error_type):
    def handler(value):
        raise error_type("handler failure")

    tool = Tool("numeric", "test numeric", handler, {"value": {"type": "integer"}})
    with pytest.raises(error_type, match="handler failure"):
        call_tool(tool, {"value": 2}, ToolContext(cwd=tmp_path))


def test_async_call_uses_same_argument_guard(tmp_path):
    import asyncio

    calls = []

    async def handler(value):
        calls.append(value)
        return "ok"

    tool = Tool("numeric", "test numeric", handler, {"value": {"type": "number"}})
    result = asyncio.run(call_tool_async(tool, {"value": float("nan")}, ToolContext(cwd=tmp_path)))
    assert result.startswith("ERROR:")
    assert not calls
