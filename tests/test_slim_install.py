"""Optional feature boundaries must not weaken the core tool/runtime imports."""

import builtins
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from js import settings, turn_settings
from js.toolkit import ToolContext
from js.toolkit import browser, kernel, terminal, toolbox
from js.toolkit.registry import build_default_registry


def test_core_imports_without_optional_packages():
    script = '''
import importlib.abc
import sys
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PIL', 'ipykernel', 'jupyter_client', 'zmq', 'typesafe_sdk', 'playwright'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, BlockOptional())
import js.cli
import js.runtime
import js.model_client
from js.toolkit import ToolContext, call_tool
from js.toolkit.registry import build_default_registry
from js.tool_args import schema_error
registry = build_default_registry().with_kernel(False)
assert all(registry.resolve(name) for name in ('shell', 'read', 'write', 'fs_search'))
assert schema_error({'x': 'bad'}, {'type': 'object', 'properties': {'x': {'type': 'integer'}}})
result = call_tool(registry.resolve('shell'), {'command': 'true'}, ToolContext())
assert 'ERROR:' not in result
'''
    subprocess.run([sys.executable, "-c", script], check=True,
                   cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)


def test_kernel_setting_projects_installs_and_is_inherited():
    store = settings.seed_defaults()
    assert turn_settings.project(store)["kernel_enabled"] is False
    settings.set_dotted(store, ("kernel", "enabled"), True)
    cfg = SimpleNamespace(**turn_settings.project(store))
    parent = ToolContext()
    turn_settings.install(parent, cfg)
    child = ToolContext(**turn_settings.inherit(parent))
    assert child.kernel_enabled is True


def test_live_kernel_toggle_rebuilds_the_selected_surface(tmp_path):
    from js import cli, config, setcmd

    cfg = config.from_env()
    state = {"settings": settings.seed_defaults(),
             "tool_selectors": ("kernel:eager", "toolbox:eager", "shell:eager")}
    for enabled in (True, False, True):
        settings.set_dotted(state["settings"], ("kernel", "enabled"), enabled)
        result = setcmd.CommandResult(changed_keys=("kernel.enabled",))
        cli._apply_settings_result(result, state, cfg)
        surface = state["tool_registry"]
        assert (surface.resolve("kernel") is not None) is enabled
        assert (surface.resolve("toolbox") is not None) is enabled
        assert surface.resolve("shell") is not None


def test_disabled_kernel_hides_discovery_and_refuses_direct_calls(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled feature touched the kernel or toolbox storage")

    monkeypatch.setattr(kernel, "missing_dependencies", forbidden)
    monkeypatch.setattr(toolbox, "seed_examples", forbidden)
    registry = build_default_registry()
    assert registry.with_kernel(True).resolve("kernel") is not None
    assert registry.with_kernel(True).resolve("toolbox") is not None
    disabled = registry.with_kernel(False)
    surface = disabled.lazy_surface(tmp_path)
    assert not {"kernel", "toolbox"} & set(surface.by_name)
    assert not {"kernel", "toolbox"} & {entry.name for entry in surface.catalog()}
    context = ToolContext(cwd=tmp_path)
    assert kernel.kernel(code="1", context=context).startswith("ERROR:")
    assert toolbox.toolbox(context=context).startswith("ERROR:")
    assert kernel.get_session(context)[0] is None


@pytest.mark.parametrize("feature", ["vision", "browser", "tags"])
def test_optional_feature_reports_its_install_extra(feature, monkeypatch, tmp_path):
    original = builtins.__import__

    def without_optional(name, *args, **kwargs):
        if name.split(".")[0] in {"PIL", "typesafe_sdk"}:
            raise ModuleNotFoundError(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_optional)
    with pytest.raises(RuntimeError):
        if feature == "vision":
            terminal._draw_screen(None, tmp_path / "screen.png")
        elif feature == "browser":
            browser._frame_metrics(b"")
        else:
            from js import session_tags
            session_tags.judge({}, {}, model="test")
