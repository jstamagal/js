from __future__ import annotations

import os
from types import SimpleNamespace

from js import cli, persona
from js.sampling import Sampling, call_params


_JS_SAMPLING_ENV = ("JS_TEMP", "JS_TOPP", "JS_TOPK", "JS_REPPEN", "JS_PRPEN")


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
            "JS_TEMP": "0.6",
            "JS_TOPP": "0.95",
            "JS_TOPK": "64",
            "JS_REPPEN": "1.05",
            "JS_PRPEN": "1.2",
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
    (prompts / "00-tools.yaml").write_text(
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
    assert all(name not in os.environ for name in _JS_SAMPLING_ENV)
