"""Sampling flags reach the provider request after the configuration layers."""

import json

import ai
import pytest

from js import cli, model_client, request_params, runtime, sampling

VALUES = {
    "temperature": 0.7, "top_p": 0.9, "top_k": 50, "min_p": 0.05,
    "top_n_sigma": 1.0, "repetition_penalty": 1.1, "presence_penalty": 1.2,
    "xtc_probability": 0.5, "xtc_threshold": 0.1, "dry_multiplier": 0.8,
    "dry_base": 1.75, "dry_allowed_length": 2, "dry_penalty_last_n": -1,
    "dry_sequence_breakers": ["\n", ":"],
}


def _capture(monkeypatch):
    requests = []
    def stream(**kw):
        request = request_params.build_request_params(
            model_id=kw["model_id"], provider_id=kw["provider_id"],
            provider_base_url=kw["provider_base_url"], max_output_tokens=kw["max_output_tokens"],
            reasoning_effort=kw["reasoning_effort"], sampling=kw["sampling"], settings=kw["settings"],
        )
        sent = dict(request.extra_body or {})
        for sampler in (request.sampling if isinstance(request.sampling, dict) else {}).values():
            for key in ("temperature", "top_p", "presence_penalty"):
                value = getattr(sampler, key, None)
                if value is not None:
                    sent[key] = value
        requests.append(sent)
        return model_client.ModelStreamResult(text="ok", tool_calls=[], reasoning="", usage=None,
                                             finish_reason="stop", assistant_message=ai.assistant_message("ok"))
    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    return requests


@pytest.mark.parametrize("field", sampling.SAMPLING_FIELDS)
def test_each_sampling_flag_reaches_the_wire(field, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    requests = _capture(monkeypatch)
    value = json.dumps(VALUES[field])
    assert cli.main(["-p", "hi", "--no-save", "--url", "openai+http://model@local.test/v1",
                     "--" + field.replace("_", "-"), value]) == 0
    assert requests[0][field] == VALUES[field]


@pytest.mark.parametrize("source,expected", [("jsrc", 0.1), ("agent", 0.2), ("extra", 0.3), ("flag", 0.4)])
def test_sampling_precedence_at_the_wire(source, expected, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".js" / "agents" / "sampling-test"
    directory.mkdir(parents=True)
    (directory / "01-prompt.md").write_text("Answer briefly.")
    (tmp_path / ".js" / "jsrc").write_text("set TEMPERATURE 0.1\n")
    if source != "jsrc":
        (directory / "agent.yaml").write_text("sampling:\n  temperature: 0.2\n")
    argv = ["--agent", "sampling-test", "-p", "hi", "--no-save", "--url", "openai+http://model@local.test/v1"]
    if source in ("extra", "flag"):
        argv += ["--extra", "TEMPERATURE=0.3"]
    if source == "flag":
        # Even an --extra appearing after the flag has lower precedence.
        argv += ["--temperature", "0.4", "--extra", "TEMPERATURE=0.3"]
    requests = _capture(monkeypatch)
    assert cli.main(argv) == 0
    assert requests[0]["temperature"] == expected


@pytest.mark.parametrize("field", sampling.SAMPLING_FIELDS)
def test_invalid_sampling_values_name_the_flag(field, capsys):
    flag = "--" + field.replace("_", "-")
    assert cli.main(["-p", "hi", flag, "invalid"]) == 2
    assert flag in capsys.readouterr().err
