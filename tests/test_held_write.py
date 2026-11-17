"""A failed write keeps its content held; write with from_held finishes it."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest
from jail_probe import bwrap_starts

from js import held, jail, runtime
from js.toolkit import ToolContext, call_tool
from js.toolkit.registry import build_default_registry

CONTENT = "".join(f"line {n}: {'x' * 40}\n" for n in range(40))


def tool(name: str, context: ToolContext, **args) -> str:
    return call_tool(build_default_registry().resolve(name), args, context)


def held_id(result: str) -> str:
    match = re.search(r"held id (h\d+)", result)
    assert match, result
    return match.group(1)


@pytest.fixture
def jailed(tmp_path, monkeypatch):
    if not bwrap_starts():
        pytest.skip("bubblewrap cannot start a jail here")
    root = tmp_path / "specbench"
    (root / "sortsmith").mkdir(parents=True)
    monkeypatch.chdir(root)
    jail.enter(root)
    context = ToolContext(cwd=root)
    context.jail_bind = ()
    return context


def test_a_jailed_write_is_held_and_from_held_writes_it_to_the_suggested_path(jailed, tmp_path):
    outside = tmp_path / "user" / "sortsmith" / "sortsmith.py"

    refused = tool("write", jailed, file_path=str(outside), content=CONTENT)

    assert refused.startswith("ERROR")
    assert not outside.exists()
    ident = held_id(refused)
    kept = held.load(jailed, ident)
    assert kept.text == CONTENT
    assert kept.path.parent.parent == jail.active().js_tmp
    assert stat.S_IMODE(kept.path.stat().st_mode) == 0o600
    corrected = jailed.cwd / "sortsmith" / "sortsmith.py"
    assert json.dumps(str(corrected)) in refused

    done = tool("write", jailed, file_path=str(corrected), from_held=ident)

    assert not done.startswith("ERROR"), done
    assert corrected.read_bytes() == CONTENT.encode("utf-8")
    assert not kept.path.exists()
    with pytest.raises(held.HeldError):
        held.load(jailed, ident)


def test_an_unread_existing_file_holds_the_content_until_read_and_from_held(tmp_path):
    context = ToolContext(cwd=tmp_path, file_require_read=True)
    target = tmp_path / "notes.txt"
    target.write_text("old\n")

    refused = tool("write", context, file_path=str(target), content=CONTENT)

    assert refused.startswith("ERROR")
    assert target.read_text() == "old\n"
    ident = held_id(refused)
    kept = held.load(context, ident)
    assert kept.path.exists()

    again = tool("write", context, file_path=str(target), from_held=ident)
    assert again.startswith("ERROR")
    assert held_id(again) == ident
    assert kept.path.exists()

    tool("read", context, file_path=str(target))
    done = tool("write", context, file_path=str(target), from_held=ident)

    assert not done.startswith("ERROR"), done
    assert target.read_bytes() == CONTENT.encode("utf-8")
    assert not kept.path.exists()


def test_a_committed_held_id_never_names_later_content(tmp_path):
    context = ToolContext(cwd=tmp_path, file_require_read=True)
    target = tmp_path / "notes.txt"
    target.write_text("old\n")

    first = held_id(tool("write", context, file_path=str(target), content=CONTENT))
    tool("read", context, file_path=str(target))
    assert not tool("write", context, file_path=str(target), from_held=first).startswith("ERROR")

    other = tmp_path / "other.txt"
    other.write_text("x\n")
    second = held_id(tool("write", context, file_path=str(other), content=CONTENT.upper()))

    assert second != first
    with pytest.raises(held.HeldError):
        held.load(context, first)
    stale = tool("write", context, file_path=str(target), from_held=first)
    assert stale.startswith("ERROR")
    assert target.read_bytes() == CONTENT.encode("utf-8")


def test_short_content_is_not_held(tmp_path):
    context = ToolContext(cwd=tmp_path, file_require_read=True)
    target = tmp_path / "notes.txt"
    target.write_text("old\n")

    refused = tool("write", context, file_path=str(target), content="short\n")

    assert refused.startswith("ERROR")
    assert "held id" not in refused


def test_truncated_write_arguments_hold_the_prefix_and_append_finishes_it(tmp_path):
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "big.py"
    whole = json.dumps({"file_path": str(target), "content": CONTENT})
    cut = whole[: whole.index("line 30")]
    prefix = CONTENT[: CONTENT.index("line 30")]
    calls = [runtime._PendingToolCall("w", "write", [cut])]

    normalized, _stats = runtime._normalize_tool_call_batch(calls, build_default_registry(), 8, context)

    error = normalized[0].validation_error
    assert error is not None
    ident = held_id(error)
    assert held.load(context, ident).text == prefix
    assert not target.exists()

    done = tool("write", context, file_path=str(target), from_held=ident, append=True,
                content=CONTENT[len(prefix):])

    assert not done.startswith("ERROR"), done
    assert target.read_bytes() == CONTENT.encode("utf-8")
    with pytest.raises(held.HeldError):
        held.load(context, ident)


def test_a_prefix_cut_inside_an_escape_drops_the_broken_escape():
    raw = '{"file_path": "a.py", "content": "one\\ntwo\\u00e'
    found, partial = held.salvage_arguments(raw)
    assert partial
    assert found == {"file_path": "a.py", "content": "one\ntwo", "append": False}


def test_write_unavailable_on_the_surface_holds_the_content(tmp_path):
    context = ToolContext(cwd=tmp_path)
    registry = build_default_registry().select(["read:eager"])
    args = json.dumps({"file_path": str(tmp_path / "x.py"), "content": CONTENT})

    _args, result = runtime._dispatch("write", args, runtime.Telemetry(None), cap_bytes=0,
                                      registry=registry, tool_context=context)

    assert held.load(context, held_id(result)).text == CONTENT


def test_append_adds_to_a_read_file(tmp_path):
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "log.txt"
    target.write_text("a\n")
    tool("read", context, file_path=str(target))

    done = tool("write", context, file_path=str(target), content="b\n", append=True)

    assert not done.startswith("ERROR"), done
    assert target.read_text() == "a\nb\n"


def test_expired_held_content_is_removed(tmp_path):
    context = ToolContext(cwd=tmp_path, held_ttl=60)
    old = held.hold(context, CONTENT, "old.py")
    past = old.path.stat().st_mtime - 3600
    os.utime(old.path, (past, past))

    held.hold(context, CONTENT + "more\n", "new.py")

    assert not Path(old.path).exists()
