"""Swarm timing inputs are validated before isolated room state changes."""

import math

import pytest

from js import swarm
from js.toolkit import ToolContext


@pytest.mark.parametrize("ttl", [math.nan, math.inf, -math.inf, 1e300])
@pytest.mark.parametrize("rail", ["claim", "board"])
def test_invalid_claim_expiry_leaves_serialized_state_unchanged(tmp_path, ttl, rail):
    room = swarm.Room(tmp_path / "bus")
    room.claim("existing", "alice", 60)
    cell = room.post_cell("alice", "work")
    before = {path.name: path.read_bytes() for path in room.root.glob("*.json")}
    with pytest.raises(ValueError):
        if rail == "claim":
            room.claim("key", "alice", ttl)
        else:
            room.take_cell(cell.id, "alice", ttl)
    assert {path.name: path.read_bytes() for path in room.root.glob("*.json")} == before
    assert "key" not in room.claims()


def test_claim_tool_rejects_bad_expiry_without_mutation(tmp_path):
    agent = swarm.Agent(tmp_path / "bus" / "alice")
    context = ToolContext(cwd=tmp_path)
    context.swarm = agent
    before = agent.room.claims()
    result = swarm._claim("key", ttl=1e300, context=context)
    assert result.startswith("ERROR")
    assert agent.room.claims() == before


@pytest.mark.parametrize("ttl,expected", [(60, 160), (0, 101), (-5, 101), (0.25, 101)])
def test_claim_finite_duration_keeps_minimum_one_second(tmp_path, monkeypatch, ttl, expected):
    room = swarm.Room(tmp_path / "bus")
    monkeypatch.setattr(swarm.time, "time", lambda: 100.0)
    assert room.claim("key", "alice", ttl) == (True, "alice", expected)
