"""Discovery validates filters before activating catalog entries."""

import asyncio
import json

import pytest

from js.toolkit.core import CatalogEntry
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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("source", ["Straße", "STRASSE", "straße", "other"])
def test_source_filter_uses_casefolded_exact_match(tmp_path, monkeypatch, asynchronous, source):
    surface = TurnToolSurface(ToolRegistry((), {}), tmp_path)
    entry = CatalogEntry("native:sample", "sample", "sample tool", "native", "Straße")
    monkeypatch.setattr(surface, "catalog", lambda: (entry,))

    if asynchronous:
        result = asyncio.run(surface.discover_async(source=source))
    else:
        result = surface.discover(source=source)

    catalog = json.loads(result)
    assert catalog["total"] == (0 if source == "other" else 1)
    if source != "other":
        assert catalog["results"][0]["id"] == entry.id
