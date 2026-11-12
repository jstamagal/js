import copy

import pytest

from js import setcmd


@pytest.mark.parametrize("key", [
    "EXTRA_PARAMS.", ".EXTRA_PARAMS", "EXTRA_PARAMS..organization",
    "EXTRA_PARAMS.organization.", "EXTRA_PARAMS...organization", ".MODEL", "MODEL.", "",
])
@pytest.mark.parametrize("operation", ["set", "unset"])
def test_malformed_setting_path_is_rejected_without_mutation(key, operation):
    state = {"EXTRA_PARAMS": {"organization": "a", "keep": "1"}, "MODEL": "test-model"}
    before = copy.deepcopy(state)
    baseline = {"EXTRA_PARAMS": {"organization": "baseline", "keep": "baseline"}}
    baseline_before = copy.deepcopy(baseline)
    result = (setcmd.apply_set(state, key, "hello") if operation == "set"
              else setcmd.apply_unset(state, key, baseline))
    assert result.handled
    assert result.error is not None
    assert not result.changed
    assert result.changed_keys == []
    assert state == before
    assert baseline == baseline_before


def test_valid_dotted_setting_set_and_unset_preserve_siblings():
    state = {"EXTRA_PARAMS": {"organization": "a", "keep": "1"}}
    result = setcmd.apply_set(state, "EXTRA_PARAMS.organization", "hello")
    assert result.changed and result.error is None
    assert state == {"EXTRA_PARAMS": {"organization": "hello", "keep": "1"}}
    result = setcmd.apply_unset(state, "EXTRA_PARAMS.organization")
    assert result.changed and result.error is None
    assert state == {"EXTRA_PARAMS": {"keep": "1"}}


@pytest.mark.parametrize("line", ["EXTRA_PARAMS. hello", "-EXTRA_PARAMS."])
def test_set_command_reports_malformed_path(line):
    state = {"EXTRA_PARAMS": {"keep": "1"}}
    result = setcmd.set_command(state, line)
    assert result.error is not None
    assert state == {"EXTRA_PARAMS": {"keep": "1"}}
