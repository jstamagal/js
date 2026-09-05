"""Suite-wide fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stock_tool_descriptions(monkeypatch):
    """Tests pin the stock description text unless they ask for a variant, so the
    operator's own `JS_TOOL_DESCRIPTIONS=slim` must not leak into the suite."""
    monkeypatch.delenv("JS_TOOL_DESCRIPTIONS", raising=False)
    monkeypatch.delenv("JS_TOOLS_DESCRIPTIONS", raising=False)
