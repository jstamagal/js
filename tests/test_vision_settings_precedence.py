"""Vision uses the resolved settings layer, including CLI and live overrides."""

from copy import deepcopy

import pytest

from js import cli, config, model_metadata, setcmd


@pytest.mark.parametrize(("environment", "override", "expected"), [
    ("off", "on", True),
    ("on", "off", False),
])
def test_cli_extra_overrides_vision_environment(monkeypatch, environment, override, expected):
    monkeypatch.setenv("JS_VISION", environment)
    monkeypatch.setattr(model_metadata, "accepts_image_input", lambda _model: False)
    cfg = config.from_env(extras=[
        "MODEL=private-model",
        "PROVIDER=openai",
        "API_KEY=fixture",
        "OUTPUT_TOKENS_MAX=64",
        f"VISION={override}",
    ])
    assert cfg.vision_enabled is expected


def test_live_vision_setting_and_unset_override_environment(monkeypatch):
    monkeypatch.setenv("JS_VISION", "off")
    monkeypatch.setattr(model_metadata, "accepts_image_input", lambda _model: True)
    cfg = config.from_env(extras=[
        "MODEL=private-model", "PROVIDER=openai", "API_KEY=fixture",
        "OUTPUT_TOKENS_MAX=64",
    ])
    assert cfg.vision_enabled is False
    live = deepcopy(cfg.settings)
    state = {"settings": live}
    assert setcmd.set_command(live, "VISION on").error is None
    active = cli._cfg_for_live_state(cfg, state)
    assert active.vision_enabled is True
    assert config.vision_enabled_for_model(active.model, active.settings) is True
    assert setcmd.set_command(live, "-VISION").error is None
    active = cli._cfg_for_live_state(cfg, state)
    assert active.vision_enabled is True
    assert config.vision_enabled_for_model(active.model, active.settings) is True
