"""Discovery validates filters before activating catalog entries."""

import asyncio

import pytest

from js.toolkit.registry import ToolRegistry, TurnToolSurface


@pytest.mark.parametrize("asynchronous", [False, True])
def test_invalid_kind_does_not_load_entry(tmp_path, monkeypatch, asynchronous):
    surface = TurnToolSurface(ToolRegistry((), {}), tmp_path)
    loaded = []
    monkeypatch.setattr(surface, "_load", lambda item_id: loaded.append(item_id) or "loaded")
    arguments = {"kind": "bogus", "load": "native:missing"}

    if asynchronous:
        result = asyncio.run(surface.discover_async(**arguments))
    else:
        result = surface.discover(**arguments)

    assert loaded == []
    assert result.startswith("ERROR")
