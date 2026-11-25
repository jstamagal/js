"""Coercion of model-supplied tool arguments keeps invalid values on defaults."""

import json

import pytest

from js.toolkit.sanitize import int_or_default


@pytest.mark.parametrize("encoded", ["Infinity", "-Infinity", "1e400", "-1e400", "NaN"])
def test_int_or_default_rejects_nonfinite_json_numbers(encoded):
    assert int_or_default(json.loads(encoded), -1, minimum=0) == -1


@pytest.mark.parametrize("raw", [None, True, False, "invalid", "Infinity", [], {}])
def test_int_or_default_rejects_invalid_values(raw):
    assert int_or_default(raw, -1, minimum=0) == -1


@pytest.mark.parametrize("raw, expected", [(0, 0), (2.9, 2), ("3", 3), (-1, -1)])
def test_int_or_default_preserves_valid_conversion(raw, expected):
    assert int_or_default(raw, 7) == expected


def test_int_or_default_respects_minimum():
    assert int_or_default(-1, 7, minimum=0) == 7
    assert int_or_default(0, 7, minimum=0) == 0
