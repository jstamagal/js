"""Empty no-argument calls are valid even when argument repair is disabled."""

import json

import pytest

from js import settings
from js.tool_args import repair_jsonish


@pytest.mark.parametrize("strict", [False, True])
def test_empty_tool_arguments_are_an_empty_object(strict):
    settings.bind_live_settings({"TOOL_ARGUMENT_STRICT": strict})
    assert repair_jsonish("") == {}


@pytest.mark.parametrize("raw", [" ", "{", '{"count": 1,}', "not json"])
def test_strict_nonempty_malformed_arguments_are_not_repaired(raw):
    settings.bind_live_settings({"TOOL_ARGUMENT_STRICT": True})
    with pytest.raises(json.JSONDecodeError):
        repair_jsonish(raw)


@pytest.mark.parametrize("raw", ["[]", "null", "false", '""'])
def test_strict_nonobject_arguments_remain_invalid(raw):
    settings.bind_live_settings({"TOOL_ARGUMENT_STRICT": True})
    with pytest.raises(ValueError):
        repair_jsonish(raw)


def test_strict_valid_object_arguments_are_preserved():
    settings.bind_live_settings({"TOOL_ARGUMENT_STRICT": True})
    assert repair_jsonish('{"count": "1"}') == {"count": "1"}
