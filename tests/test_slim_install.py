"""Optional feature boundaries must not weaken the core tool/runtime imports."""

import builtins
from types import SimpleNamespace

import pytest

from js import settings, turn_settings
from js.toolkit import ToolContext
from js.toolkit import browser, kernel, terminal, toolbox
from js.toolkit.registry import build_default_registry


def test_kernel_setting_projects_installs_and_is_inherited():
    store = settings.seed_defaults()
    assert turn_settings.project(store)["kernel_enabled"] is False
    settings.set_dotted(store, ("PYTHON_KERNEL",), True)
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
        settings.set_dotted(state["settings"], ("PYTHON_KERNEL",), enabled)
        result = setcmd.CommandResult(changed_keys=("PYTHON_KERNEL",))
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


@pytest.mark.parametrize("feature", ["vision", "browser"])
def test_optional_feature_reports_its_install_extra(feature, monkeypatch, tmp_path):
    original = builtins.__import__

    def without_optional(name, *args, **kwargs):
        if name.split(".")[0] == "PIL":
            raise ModuleNotFoundError(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_optional)
    with pytest.raises(RuntimeError):
        if feature == "vision":
            terminal._draw_screen(None, tmp_path / "screen.png")
        else:
            browser._frame_metrics(b"")
