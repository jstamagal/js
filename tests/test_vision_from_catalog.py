"""Vision capability comes from models.dev modalities, keyed on the MODEL, not the
provider. The same model reached through cliproxyapi, a local llama.cpp server, or
a router prefix has the same input modalities; a hardcoded name list goes stale the
day a new generation ships."""

from __future__ import annotations


from js import model_metadata
from js.config import vision_enabled_for_model


def test_catalog_models_report_their_real_image_support():
    # Reached through arbitrary provider prefixes: capability follows the model.
    assert vision_enabled_for_model("cliproxyapi/claude-opus-5") is True
    assert vision_enabled_for_model("claude-opus-5") is True
    assert vision_enabled_for_model("openai/qwen3.8:27B") is True
    assert vision_enabled_for_model("9router/cx/gpt-5.6-sol") is True


def test_an_effort_suffix_keeps_the_models_image_support():
    # No name hint covers kimi: only the catalog row can say yes.
    assert vision_enabled_for_model("cpa/kimi-k3-high", {}) is True
    assert vision_enabled_for_model("cpa/antigravity/kimi-k3-low", {}) is True


def test_text_only_models_stay_disabled():
    assert vision_enabled_for_model("deepseek/deepseek-v4-pro") is False


def test_the_makers_modalities_beat_a_resellers(monkeypatch):
    rows = (
        model_metadata._ModelRow("zai", "glm-9", 1_000_000, 131072, None, "glm", "2027-01-01", False),
        model_metadata._ModelRow("resellerx", "glm-9", 1_000_000, 131072, None, "glm", "2027-01-01", True),
    )
    monkeypatch.setattr(model_metadata, "_all_models", lambda: rows)

    assert model_metadata.accepts_image_input("cpa/glm-9-high", refresh=lambda: None) is False


def test_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("JS_VISION", "0")
    assert vision_enabled_for_model("claude-opus-5") is False
    monkeypatch.setenv("JS_VISION", "1")
    assert vision_enabled_for_model("deepseek/deepseek-v4-pro") is True


def test_unknown_models_fall_back_to_the_name_heuristic():
    assert vision_enabled_for_model("some-private-build-vl") is True
    assert vision_enabled_for_model("some-private-build") is False


def test_knob_forces_vision_on_or_off():
    assert vision_enabled_for_model("deepseek/deepseek-v4-pro", {"VISION": True}) is True
    assert vision_enabled_for_model("claude-opus-5", {"VISION": False}) is False
    assert vision_enabled_for_model("some-private-build", {"VISION": True}) is True
