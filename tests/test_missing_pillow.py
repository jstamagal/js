"""Without Pillow, terminal_snapshot and browser_probe raise one error, not a traceback."""

import builtins

import pytest

from js.toolkit import browser, terminal


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
