"""apply_unset mirrors apply_set's guard: children of non-map knobs are not
unset targets, and the "already unset" message follows the real change.

ALL-CLAIMS setcmd.py HIGH x2 + MED: `set -MCP_SERVERS.foo` leaked secret
headers with raw str(after) and mutated nothing-but-said-changed; unsetting
a child of a scalar knob (MODEL.child) was accepted while setting the
same path errors.
"""

from js import setcmd


def test_unset_rejects_children_of_json_knob():
    baseline = {"MCP_SERVERS": {"foo": {"headers": {"Authorization": "Bearer SECRET"}}}}
    result = setcmd.apply_unset({}, "MCP_SERVERS.foo", baseline)
    assert result.error is not None
    assert not result.changed
    # nothing leaked
    assert all("Bearer" not in line for line in result.lines)


def test_unset_rejects_children_of_scalar_knob_like_apply_set():
    baseline = {"MODEL": "basemodel"}
    assert setcmd.apply_set({"MODEL": {"child": "x"}}, "MODEL.child", "x").error is not None
    result = setcmd.apply_unset({"MODEL": {"child": "x"}}, "MODEL.child", baseline)
    assert result.error is not None
    assert not result.changed
    assert setcmd.apply_unset({}, "MODEL.child", baseline).error is not None


def test_unset_message_follows_changed():
    # restored from baseline: changed, so the value line, not "already unset"
    restored = setcmd.apply_unset({}, "model", {"MODEL": "basemodel"})
    assert restored.changed
    assert "Already unset" not in restored.lines[0]

    # nothing to do: not changed, "already unset"
    same = setcmd.apply_unset({"MODEL": "basemodel"}, "model", {"MODEL": "basemodel"})
    assert not same.changed
    assert "Already unset" in same.lines[0]

    # absent everywhere: not changed, "already unset"
    absent = setcmd.apply_unset({}, "model", {})
    assert not absent.changed
    assert "Already unset" in absent.lines[0]


def test_unset_of_map_child_still_works():
    result = setcmd.apply_unset(
        {"EXTRA_PARAMS": {"organization": "acme"}},
        "EXTRA_PARAMS.organization",
        {"EXTRA_PARAMS": {}},
    )
    assert result.changed
    assert not result.error
