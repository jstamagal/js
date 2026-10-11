"""apply_unset mirrors apply_set's guard: children of non-map knobs are not
unset targets, and the "already unset" message follows the real change.

ALL-CLAIMS setcmd.py HIGH x2 + MED: `set -mcp.servers.foo` leaked secret
headers with raw str(after) and mutated nothing-but-said-changed; unsetting
a child of a scalar knob (model.id.child) was accepted while setting the
same path errors.
"""

from js import setcmd


def test_unset_rejects_children_of_json_knob():
    baseline = {"mcp": {"servers": {"foo": {"headers": {"Authorization": "Bearer SECRET"}}}}}
    result = setcmd.apply_unset({}, "mcp.servers.foo", baseline)
    assert result.error is not None
    assert not result.changed
    # nothing leaked
    assert all("Bearer" not in line for line in result.lines)


def test_unset_rejects_children_of_scalar_knob_like_apply_set():
    baseline = {"model": {"id": "basemodel"}}
    assert setcmd.apply_set({"model": {"id": {"child": "x"}}}, "model.id.child", "x").error is not None
    result = setcmd.apply_unset({"model": {"id": {"child": "x"}}}, "model.id.child", baseline)
    assert result.error is not None
    assert not result.changed
    assert setcmd.apply_unset({}, "model.id.child", baseline).error is not None


def test_unset_message_follows_changed():
    # restored from baseline: changed, so the value line, not "already unset"
    restored = setcmd.apply_unset({}, "model", {"model": {"id": "basemodel"}})
    assert restored.changed
    assert "Already unset" not in restored.lines[0]

    # nothing to do: not changed, "already unset"
    same = setcmd.apply_unset({"model": {"id": "basemodel"}}, "model", {"model": {"id": "basemodel"}})
    assert not same.changed
    assert "Already unset" in same.lines[0]

    # absent everywhere: not changed, "already unset"
    absent = setcmd.apply_unset({}, "model", {})
    assert not absent.changed
    assert "Already unset" in absent.lines[0]


def test_unset_of_map_child_still_works():
    result = setcmd.apply_unset(
        {"provider": {"extra": {"organization": "acme"}}},
        "provider.extra.organization",
        {"provider": {"extra": {}}},
    )
    assert result.changed
    assert not result.error
