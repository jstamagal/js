"""A kernel cell calls js tools: `tools.read(...)` returns a Python value.

These start real kernels. A cell's tool calls run in the js process through
the registry the kernel call was dispatched through, with the live
ToolContext, so the tests go through runtime._dispatch the way a model's
call does.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace

import pytest

from js import runtime
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry

needs_kernel = pytest.mark.skipif(
    not all(importlib.util.find_spec(name) for name in ("jupyter_client", "ipykernel")),
    reason="jupyter_client/ipykernel not installed",
)


@pytest.fixture
def ctx(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    context = ToolContext(cwd=work)
    context.kernel_verbosity = "quiet"
    context.kernel_wait_seconds = 30
    yield context
    session = context.kernel_session
    if session is not None:
        session.shutdown()


def dispatch(name: str, context: ToolContext, registry=None, **args) -> str:
    _, result = runtime._dispatch(
        name, json.dumps(args), runtime.Telemetry(None), cap_bytes=256 * 1024,
        registry=registry or build_default_registry(), tool_context=context,
    )
    return result


def cell(code: str, context: ToolContext, registry=None) -> str:
    return dispatch("kernel", context, registry, code=code)


@needs_kernel
def test_one_cell_calls_two_tools_and_gets_python_values(ctx):
    (ctx.cwd / "notes.txt").write_text("alpha\nneedle here\n")
    (ctx.cwd / "other.txt").write_text("nothing\n")

    result = cell(
        "text = tools.read('notes.txt', show_line_numbers=False)\n"
        "found = tools.fs_search(pattern='needle', output_mode='files_with_matches')\n"
        "print('TYPES', type(text).__name__, type(found).__name__)\n"
        "print('READ', text.splitlines()[-1])\n"
        "print('FOUND', 'notes.txt' in found, 'other.txt' in found)\n",
        ctx,
    )

    assert "TYPES str str" in result
    assert "READ needle here" in result
    assert "FOUND True False" in result


@needs_kernel
def test_a_read_from_a_cell_counts_as_read_for_patch(ctx):
    target = ctx.cwd / "code.py"
    target.write_text("x = 1\n")

    cell("tools.read(file_path='code.py')", ctx)
    patched = dispatch("patch", ctx, file_path="code.py", old_string="x = 1", new_string="x = 2")

    assert ctx.resolve_path("code.py") in ctx.read_paths
    assert not patched.startswith("ERROR"), patched
    assert target.read_text() == "x = 2\n"


@needs_kernel
def test_a_cell_cannot_patch_a_file_nobody_read(ctx):
    target = ctx.cwd / "code.py"
    target.write_text("x = 1\n")

    result = cell(
        "try:\n"
        "    tools.patch(file_path='code.py', old_string='x = 1', new_string='x = 2')\n"
        "except tools.ToolError as exc:\n"
        "    print('REFUSED', exc)\n",
        ctx,
    )

    assert "REFUSED ERROR" in result
    assert target.read_text() == "x = 1\n"


@needs_kernel
def test_a_cell_reaches_only_the_tools_the_agent_may_call(ctx):
    (ctx.cwd / "note.txt").write_text("secret-content-4411\n")
    surface = build_default_registry().select(["kernel:eager"], warn=False)

    result = cell(
        "try:\n"
        "    print(tools.read('note.txt'))\n"
        "except tools.ToolError as exc:\n"
        "    print('REFUSED')\n"
        "print('NAMES', tools.names())\n",
        ctx, surface,
    )

    assert "REFUSED" in result
    assert "secret-content-4411" not in result
    assert "NAMES []" in result


@needs_kernel
def test_argument_bans_apply_to_a_cell(ctx):
    (ctx.cwd / "private.txt").write_text("banned-content-9120\n")
    registry = replace(build_default_registry(), arg_bans={"read": ("private",)})

    result = cell(
        "try:\n"
        "    print(tools.read('private.txt'))\n"
        "except tools.ToolError as exc:\n"
        "    print('REFUSED', exc)\n",
        ctx, registry,
    )

    assert "REFUSED ERROR" in result
    assert "banned-content-9120" not in result


@needs_kernel
def test_bad_arguments_raise_instead_of_running(ctx):
    result = cell(
        "for call in (lambda: tools.read(), lambda: tools.read('a', True, False, 'extra')):\n"
        "    try:\n"
        "        call()\n"
        "        print('RAN')\n"
        "    except tools.ToolError:\n"
        "        print('REFUSED')\n",
        ctx,
    )

    assert result.count("REFUSED") == 2
    assert "RAN" not in result


@needs_kernel
def test_a_cell_cannot_call_the_kernel_it_runs_in(ctx):
    result = cell(
        "try:\n"
        "    tools.kernel(code='1')\n"
        "except tools.ToolError as exc:\n"
        "    print('REFUSED')\n",
        ctx,
    )

    assert "REFUSED" in result


@needs_kernel
def test_tools_is_not_reported_as_something_the_agent_built(ctx):
    result = cell("def helper():\n    return 1\n", ctx)

    assert "tools" not in result.split("DEFINED", 1)[-1]
    assert result.rstrip().endswith("NAMESPACE helper")


@needs_kernel
def test_tools_survive_a_restart(ctx):
    (ctx.cwd / "notes.txt").write_text("after-restart-5521\n")
    cell("x = 1", ctx)

    dispatch("kernel", ctx, restart=True)
    result = cell("print(tools.read('notes.txt', show_line_numbers=False))", ctx)

    assert "after-restart-5521" in result


@needs_kernel
def test_a_kernel_called_outside_dispatch_refuses_tool_calls(ctx):
    from js.toolkit import kernel as kmod

    (ctx.cwd / "notes.txt").write_text("undispatched-7310\n")
    result = kmod.kernel(
        code="try:\n"
             "    print(tools.read('notes.txt'))\n"
             "except tools.ToolError:\n"
             "    print('REFUSED')\n",
        context=ctx,
    )

    assert "REFUSED" in result
    assert "undispatched-7310" not in result


@needs_kernel
def test_a_write_to_a_non_utf8_name_succeeds_and_the_bridge_keeps_serving(ctx):
    import os

    os.mkdir(os.fsencode(ctx.cwd / "names"))
    open(os.fsencode(ctx.cwd / "names") + b"/bad\xffname.txt", "wb").close()

    first = cell(
        "import os\n"
        "for n in os.listdir('names'):\n"
        "    print('WROTE', tools.write(file_path='names/' + n + '.bak', content='copy\\n')[:5])\n",
        ctx,
    )
    (ctx.cwd / "later.txt").write_text("still-serving-8812\n")
    second = cell("print(tools.read('later.txt', show_line_numbers=False))", ctx)

    assert "WROTE" in first and "ToolError" not in first, first
    assert (os.fsencode(ctx.cwd / "names") + b"/bad\xffname.txt.bak") in [
        os.fsencode(ctx.cwd / "names") + b"/" + n for n in os.listdir(os.fsencode(ctx.cwd / "names"))
    ]
    assert "still-serving-8812" in second


@needs_kernel
def test_a_cell_tool_call_is_logged_like_a_direct_call(ctx):
    (ctx.cwd / "notes.txt").write_text("logged\n")
    events: list[tuple[str, dict]] = []
    telemetry = runtime.Telemetry(None)
    telemetry.event = lambda kind, **fields: events.append((kind, fields))

    runtime._dispatch(
        "kernel", json.dumps({"code": "tools.read('notes.txt')"}), telemetry,
        cap_bytes=256 * 1024, registry=build_default_registry(), tool_context=ctx,
    )

    assert ("tool_ok", "read", "kernel") in [
        (kind, fields.get("tool"), fields.get("via")) for kind, fields in events
    ]


@needs_kernel
def test_a_dead_kernel_is_shut_down_before_its_replacement_starts(ctx):
    cell("x = 1", ctx)
    old = ctx.kernel_session
    old_bridge = old.bridge
    old.manager.shutdown_kernel(now=True)

    result = cell("print('NEW', 1 + 1)", ctx)

    assert "NEW 2" in result
    assert ctx.kernel_session is not old
    assert old.bridge is None and not old_bridge.serving()


# The bridge on its own, without a kernel: a raw client on its socket.

@pytest.fixture
def bridge():
    import shutil
    import tempfile
    from pathlib import Path

    from js.toolkit.kernel_bridge import ToolBridge

    folder = Path(tempfile.mkdtemp(prefix="jsb-"))
    served = ToolBridge(folder / "tools.sock")
    yield served
    served.close()
    shutil.rmtree(folder, ignore_errors=True)


def raw_request(bridge, payload: bytes) -> dict:
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(bridge.socket_path))
        conn.sendall(payload)
        conn.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := conn.recv(1 << 16):
            data += chunk
    return json.loads(data)


def names_request(bridge, token=None) -> bytes:
    return json.dumps({"op": "names", "token": token or bridge.token}).encode()


def test_a_request_the_bridge_cannot_parse_gets_an_error_and_serving_goes_on(bridge, tmp_path):
    bridge.attach(build_default_registry(), ToolContext(cwd=tmp_path))

    nested = raw_request(bridge, b"[" * 100_000 + b"]" * 100_000)
    foreign = raw_request(bridge, json.dumps({"op": "names", "token": "é" * 32}).encode())
    good = raw_request(bridge, names_request(bridge))

    assert "error" in nested and "error" in foreign
    assert "read" in good["value"]
    assert bridge.serving()


def test_a_stopped_bridge_serves_again_on_the_same_socket_and_token(bridge, tmp_path):
    bridge.attach(build_default_registry(), ToolContext(cwd=tmp_path))
    token = bridge.token
    bridge._server.close()
    bridge._thread.join(timeout=5)
    assert not bridge.serving()

    bridge.revive()

    assert bridge.serving() and bridge.token == token
    assert "read" in raw_request(bridge, names_request(bridge))["value"]


def _guard_refusing(tool_name: str, seen: list[tuple[str, str]]):
    """An `on` table whose tool_call handler refuses ``tool_name``; ``seen``
    collects (event, tool) for every tool_call and tool_result it is shown."""
    from js import events

    hooks = events.EventHooks()

    def dispatch(hook, emission):
        name = emission.payload.get("name")
        seen.append((emission.event, name))
        if emission.event == "tool_call" and name == tool_name:
            return events.EventHandlerResult(hook=hook, refusal=f"ERROR: no {tool_name} today")
        return events.EventHandlerResult(hook=hook)

    hooks.set_dispatcher(dispatch)
    hooks.add("tool_call", "guard")
    hooks.add("tool_result", "watch")
    return hooks


@needs_kernel
def test_a_tool_call_guard_refuses_a_cells_call(ctx):
    seen: list[tuple[str, str]] = []
    ctx.tool_call_hooks = _guard_refusing("write", seen)
    (ctx.cwd / "in.txt").write_text("kept\n")

    result = cell(
        "print('READ', tools.read('in.txt', show_line_numbers=False).strip())\n"
        "try:\n"
        "    tools.write(file_path='out.txt', content='x\\n')\n"
        "except tools.ToolError as exc:\n"
        "    print('REFUSED', exc)\n",
        ctx,
    )

    assert "READ kept" in result
    assert "REFUSED ERROR: no write today" in result
    assert not (ctx.cwd / "out.txt").exists()
    assert ("tool_call", "read") in seen and ("tool_result", "read") in seen
    assert ("tool_call", "write") in seen and ("tool_result", "write") not in seen


@needs_kernel
def test_a_read_of_content_starting_with_error_is_a_value(ctx):
    (ctx.cwd / "log.txt").write_text("ERROR: disk full at 03:00\nsecond line\n")

    result = cell(
        "text = tools.read('log.txt', show_line_numbers=False)\n"
        "print('LINES', len(text.splitlines()))\n"
        "try:\n"
        "    tools.read('missing.txt')\n"
        "except tools.ToolError:\n"
        "    print('MISSING RAISED')\n",
        ctx,
    )

    assert "LINES 2" in result
    assert "MISSING RAISED" in result
