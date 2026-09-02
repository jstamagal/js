"""Vision capability comes from models.dev modalities, keyed on the MODEL, not the
provider. The same model reached through cliproxyapi, a local llama.cpp server, or
a router prefix has the same input modalities; a hardcoded name list goes stale the
day a new generation ships."""

from __future__ import annotations

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
