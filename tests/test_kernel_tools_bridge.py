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
