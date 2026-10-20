from __future__ import annotations

from types import SimpleNamespace

from js import cli, persona
from js.sampling import Sampling, call_params


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


def test_call_params_filters_anthropic_wire():
    sampling = Sampling(
        temperature=0.7,
        top_p=0.9,
        top_k=50,
        repetition_penalty=1.1,
        presence_penalty=1.2,
    )

    assert call_params(sampling, "anthropic") == {
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 50,
    }


def test_call_params_filters_openai_wire():
    sampling = Sampling(
        temperature=0.7,
        top_p=0.9,
        top_k=50,
        repetition_penalty=1.1,
        presence_penalty=1.2,
    )

    assert call_params(sampling, "openai") == {
        "temperature": 0.7,
        "top_p": 0.9,
        "presence_penalty": 1.2,
    }


def test_call_params_maps_openai_compatible_extensions_to_extra_body():
    sampling = Sampling(
        temperature=0.7,
        top_p=0.9,
        top_k=50,
        repetition_penalty=1.1,
        presence_penalty=1.2,
    )

    assert call_params(sampling, "deepseek") == {
        "temperature": 0.7,
        "top_p": 0.9,
        "presence_penalty": 1.2,
        "extra_body": {"top_k": 50, "repetition_penalty": 1.1},
    }


def test_cli_sampling_for_turn_precedence():
    cfg = SimpleNamespace(
        sampling_setscript=Sampling(temperature=0.1, top_p=0.8),
        sampling_env=Sampling(temperature=0.3, top_k=64),
    )
    prompt_spec = SimpleNamespace(
        sampling={"temperature": 0.2, "repetition_penalty": 1.05}
    )
    cli_override = Sampling(temperature=0.4, presence_penalty=1.2)

    assert cli._sampling_for_turn(cfg, prompt_spec, cli_override) == Sampling(
        temperature=0.4,
        top_p=0.8,
        top_k=64,
        repetition_penalty=1.05,
        presence_penalty=1.2,
    )


def test_call_params_unknown_transport_sends_nothing():
    sampling = Sampling(temperature=0.7, top_p=0.9, top_k=50)

    assert call_params(sampling, None) == {}
    assert call_params(sampling, "gateway") == {}


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


def test_call_params_responses_transports_send_no_presence_penalty():
    """openai's AsyncResponses.create has no presence_penalty (TypeError on
    every turn), and Codex reads only temperature and top_p."""
    sampling = Sampling(temperature=0.7, top_p=0.9, top_k=50, repetition_penalty=1.1, presence_penalty=1.2)
    for transport in ("custom_responses", "codex_oauth"):
        assert call_params(sampling, transport) == {"temperature": 0.7, "top_p": 0.9}, transport
    assert call_params(sampling, "openai")["presence_penalty"] == 1.2


def test_responses_request_params_carry_no_presence_penalty():
    from js import model_client

    sampling = Sampling(temperature=0.5, presence_penalty=1.2)
    params = model_client._build_inference_params(
        sampling, "custom_responses", reasoning=None, output=None, extra_body={})
    samplers = params.sampling or {}
    names = {type(value).__name__ for value in samplers.values()}
    assert any("Temperature" in name for name in names)
    assert not any("Presence" in name for name in names)
    assert not (params.extra_body or {}).get("presence_penalty")


import pytest  # noqa: E402


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", float("nan")])
def test_non_finite_sampling_values_are_refused(raw):
    with pytest.raises(ValueError):
        Sampling.from_mapping({"temperature": raw})
    with pytest.raises(ValueError):
        Sampling.from_env({"JS_TEMPERATURE": str(raw)})
