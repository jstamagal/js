from __future__ import annotations

from types import SimpleNamespace

from js import persona
import pytest

from js import model_client
from js.sampling import Sampling


_JS_SAMPLING_ENV = ("JS_TEMPERATURE", "JS_TOP_P", "JS_TOP_K", "JS_REPETITION_PENALTY", "JS_PRESENCE_PENALTY")


def test_sampling_merge_overlays_only_non_none_fields():
    base = Sampling(temperature=0.2, top_p=0.8, top_k=40)
    override = Sampling(temperature=0.4, top_k=None, presence_penalty=1.5)

    assert base.merge(override) == Sampling(
        temperature=0.4,
        top_p=0.8,
        top_k=40,
        presence_penalty=1.5,
    )


def test_sampling_from_env_reads_js_knobs():
    sampling = Sampling.from_env(
        {
            "JS_TEMPERATURE": "0.6",
            "JS_TOP_P": "0.95",
            "JS_TOP_K": "64",
            "JS_REPETITION_PENALTY": "1.05",
            "JS_PRESENCE_PENALTY": "1.2",
        }
    )

    assert sampling == Sampling(
        temperature=0.6,
        top_p=0.95,
        top_k=64,
        repetition_penalty=1.05,
        presence_penalty=1.2,
    )


def test_sampling_from_mapping_coerces_known_keys_and_ignores_unknown():
    sampling = Sampling.from_mapping(
        {"temperature": "0.3", "top_k": 32.0, "future": 1}
    )

    assert sampling == Sampling(temperature=0.3, top_k=32)


_EVERY = Sampling(
    temperature=0.7, top_p=0.9, top_k=50, min_p=0.05, top_n_sigma=1.0,
    xtc_probability=0.5, xtc_threshold=0.1, dry_multiplier=0.8, dry_base=1.75,
    dry_allowed_length=2, dry_penalty_last_n=-1, dry_sequence_breakers=("\n", ":"),
    repetition_penalty=1.1, presence_penalty=1.2,
)


def _sent(provider_id, sampling, model="some-model"):
    """(the wire's own sampling fields, extra_body) a request carries."""
    params = model_client.build_request_params(
        model_id=model, provider_id=provider_id, provider_base_url=None,
        max_output_tokens=None, reasoning_effort=None, sampling=sampling)
    if params is None:
        return {}, {}
    top = {}
    samplers = params.sampling if isinstance(params.sampling, dict) else {}
    for value in samplers.values():
        for name in ("temperature", "top_p", "top_k", "presence_penalty"):
            field = getattr(value, name, None)
            if isinstance(field, (int, float)) and not isinstance(field, bool):
                top[name] = field
    return top, dict(params.extra_body or {})


_NONSTANDARD = {
    "min_p": 0.05, "top_n_sigma": 1.0, "xtc_probability": 0.5, "xtc_threshold": 0.1,
    "dry_multiplier": 0.8, "dry_base": 1.75, "dry_allowed_length": 2, "dry_penalty_last_n": -1,
    "dry_sequence_breakers": ["\n", ":"], "repetition_penalty": 1.1,
}


def test_the_anthropic_wire_sends_every_set_field_in_the_body():
    top, extra = _sent("anthropic", _EVERY)

    assert top == {}
    assert {key: extra[key] for key in (*_NONSTANDARD, "presence_penalty", "temperature", "top_p", "top_k")} == {
        **_NONSTANDARD, "presence_penalty": 1.2, "temperature": 0.7, "top_p": 0.9, "top_k": 50}


@pytest.mark.parametrize("provider_id", ["openai", "deepseek", "vllm", None])
def test_the_chat_wire_sends_every_set_field(provider_id):
    top, extra = _sent(provider_id, _EVERY)

    assert top == {"temperature": 0.7, "top_p": 0.9, "presence_penalty": 1.2}
    assert {key: extra[key] for key in (*_NONSTANDARD, "top_k")} == {**_NONSTANDARD, "top_k": 50}


def test_the_responses_wire_sends_presence_penalty_in_extra_body():
    top, extra = _sent("openai-codex", Sampling(temperature=0.7, top_p=0.9, presence_penalty=1.2))

    assert top == {"temperature": 0.7, "top_p": 0.9}
    assert extra["presence_penalty"] == 1.2


@pytest.mark.parametrize("provider_id", ["anthropic", "openai", "openai-codex", "vllm", None])
def test_unset_sampling_fields_are_absent_from_the_request(provider_id):
    top, extra = _sent(provider_id, Sampling(temperature=0.3))

    assert {**top, **extra}["temperature"] == 0.3
    assert not set(extra) & {*_NONSTANDARD, "top_k", "presence_penalty", "top_p"}


def test_turn_sampling_precedence():
    from js import login_defaults

    cfg = SimpleNamespace(
        provider_id=None, provider_base_url=None, model="m",
        layer_jsrc={"TEMPERATURE": 0.1, "TOP_P": 0.8},
        layer_agent={"TEMPERATURE": 0.2, "REPETITION_PENALTY": 1.05},
        layer_env={"TEMPERATURE": 0.3, "TOP_K": 64},
        layer_set={"TEMPERATURE": 0.4, "PRESENCE_PENALTY": 1.2},
    )

    assert login_defaults.sampling(cfg) == Sampling(
        temperature=0.4,
        top_p=0.8,
        top_k=64,
        repetition_penalty=1.05,
        presence_penalty=1.2,
    )


def test_loading_prompt_spec_sampling_does_not_mutate_os_environ(monkeypatch, tmp_path):
    for name in _JS_SAMPLING_ENV:
        monkeypatch.delenv(name, raising=False)

    prompts = tmp_path / "agent"
    prompts.mkdir()
    (prompts / "agent.yaml").write_text(
        "sampling:\n"
        "  temperature: 0.2\n"
        "  top_p: 0.75\n",
        encoding="utf-8",
    )
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    cfg = SimpleNamespace(
        prompt_roots=(),
        prompts_dir=prompts,
        agents_files=(),
        allow_inline_code=False,
    )

    spec = persona.load_configured_prompt_spec(cfg)

    assert spec.sampling == {"temperature": 0.2, "top_p": 0.75}


def test_agent_yaml_sampling_takes_every_sampling_setting(tmp_path):
    prompts = tmp_path / "agent"
    prompts.mkdir()
    (prompts / "agent.yaml").write_text(
        "sampling:\n"
        "  min_p: 0.05\n"
        "  dry_allowed_length: 2\n"
        "  dry_sequence_breakers: [\"\\n\", \":\"]\n",
        encoding="utf-8",
    )
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    cfg = SimpleNamespace(prompt_roots=(), prompts_dir=prompts, agents_files=(), allow_inline_code=False)

    spec = persona.load_configured_prompt_spec(cfg)

    assert spec.sampling == {"min_p": 0.05, "dry_allowed_length": 2, "dry_sequence_breakers": ["\n", ":"]}


def test_the_sampling_keys_are_the_settings_and_the_flags():
    from js import cli, settings
    from js.sampling import SAMPLING_FIELDS

    assert set(persona._SAMPLING_KEYS) == set(SAMPLING_FIELDS)
    assert all(settings.SPEC_BY_KEY[field.upper()].group == "sampling" for field in SAMPLING_FIELDS)
    assert {(field, field.upper()) for field in SAMPLING_FIELDS} <= set(cli.SETTING_FLAGS)
