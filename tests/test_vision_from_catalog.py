"""Vision capability comes from models.dev modalities, keyed on the MODEL, not the
provider. The same model reached through cliproxyapi, a local llama.cpp server, or
a router prefix has the same input modalities; a hardcoded name list goes stale the
day a new generation ships."""

from __future__ import annotations

import os

from js.config import vision_enabled_for_model


def test_catalog_models_report_their_real_image_support():
    # Reached through arbitrary provider prefixes: capability follows the model.
    assert vision_enabled_for_model("cliproxyapi/claude-opus-5") is True
    assert vision_enabled_for_model("claude-opus-5") is True
    assert vision_enabled_for_model("openai/qwen3.8:27B") is True
    assert vision_enabled_for_model("9router/cx/gpt-5.6-sol") is True


def test_text_only_models_stay_disabled():
    assert vision_enabled_for_model("deepseek/deepseek-v4-pro") is False


def test_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("JS_VISION", "0")
    assert vision_enabled_for_model("claude-opus-5") is False
    monkeypatch.setenv("JS_VISION", "1")
    assert vision_enabled_for_model("deepseek/deepseek-v4-pro") is True


def test_unknown_models_fall_back_to_the_name_heuristic():
    assert vision_enabled_for_model("some-private-build-vl") is True
    assert vision_enabled_for_model("some-private-build") is False


def test_knob_forces_vision_on_or_off():
    assert vision_enabled_for_model("deepseek/deepseek-v4-pro", {"model": {"vision": True}}) is True
    assert vision_enabled_for_model("claude-opus-5", {"model": {"vision": False}}) is False
    assert vision_enabled_for_model("some-private-build", {"model": {"vision": True}}) is True


def test_env_override_beats_the_knob(monkeypatch):
    from js import settings

    monkeypatch.setenv("JS_VISION", "0")
    # Detection consumes the merged view; environment overrides config there.
    store = settings.apply_env_overrides({"model": {"vision": True}}, env=os.environ)
    assert vision_enabled_for_model("claude-opus-5", store) is False


def test_vision_knob_is_registered_and_settable():
    from js import settings, setcmd

    assert settings.SPEC_BY_KEY["model.vision"].type == "bool"
    store: dict = {}
    assert setcmd.run_repl_command(store, "/set model.vision on").error is None
    assert settings.get_dotted(store, ("model", "vision")) is True
    assert setcmd.run_repl_command(store, "/set -model.vision").error is None
    assert settings.get_dotted(store, ("model", "vision")) is None


def test_js_vision_env_feeds_the_knob(monkeypatch):
    from js import settings

    monkeypatch.setenv("JS_VISION", "on")
    store = settings.collect_settings(config_paths=[], env=os.environ)
    assert settings.get_dotted(store, ("model", "vision")) is True
