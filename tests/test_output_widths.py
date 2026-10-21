"""Every renderer, fed text mixing wide characters, tabs, emoji and long
unbroken runs, keeps each row inside the window in cells; the ones that wrap
rather than fold lose no visible character."""

from __future__ import annotations

import re

import pytest

from js import display, fset

SGR = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(params=["", "🇺🇸"])
def marks(request):
    """The stock speaker marks, or a flag for both."""
    if request.param:
        fset.set_entry("MARK_USER", request.param)
        fset.set_entry("MARK_AGENT", request.param)
    return request.param


@pytest.fixture(params=[20, 40, 80])
def width(request, monkeypatch):
    """The terminal's width; js draws one column short of it."""
    monkeypatch.setattr(display, "terminal_width", lambda: request.param)
    return request.param


def _every_argument(tool) -> tuple[dict, list[str]]:
    """Arguments setting every parameter of ``tool``, each to a value that
    shows a token of its own; and those tokens."""
    args, tokens = {}, []
    for index, (key, schema) in enumerate(tool.params.items()):
        kind = (schema or {}).get("type")
        token = f"ARG{index}TOKEN"
        if kind == "boolean":
            args[key], token = True, key
        elif kind in ("integer", "number"):
            args[key] = 7000 + index
            token = str(7000 + index)
        elif kind == "array":
            args[key] = [token]
        elif kind == "string":
            args[key] = token
        else:
            args[key] = {"inner": token}
        tokens.append(token)
    return args, tokens


def _tools():
    from js.toolkit.registry import registry_for_roots

    return list(registry_for_roots(None).tools)


@pytest.mark.parametrize("tool", _tools(), ids=lambda tool: tool.name)
def test_the_whole_form_of_every_tool_shows_every_one_of_its_arguments(tool):
    args, tokens = _every_argument(tool)
    for result in ("RESULTA", "ERROR: it failed"):
        shown = "".join(SGR.sub("", display.render_exchange(tool.name, args, result, level=3, width=200)).split())
        missing = [token for token in tokens if token not in shown]
        assert not missing, (tool.name, result, missing)
