from __future__ import annotations

import httpx
import pytest

from js import model_metadata, runtime


@pytest.fixture(autouse=True)
def clear_probe_cache():
    model_metadata._clear_caches()
    yield
    model_metadata._clear_caches()


def test_openai_compatible_models_payload_context_window(monkeypatch):
    calls = []

    def fake_request_json(method: str, url: str, *, json_body=None):
        calls.append((method, url, json_body))
        assert method == "GET"
        assert url == "http://vllm.test/v1/models"
        assert json_body is None
        return {
            "object": "list",
            "data": [
                {"id": "other-model", "max_model_len": 4096},
                {"id": "served-model", "max_model_len": 32768},
            ],
        }

    monkeypatch.setattr(model_metadata, "_request_json", fake_request_json)

    assert (
        model_metadata.probe_local_context_window(
            "served-model",
            "openai-completions",
            base_url="http://vllm.test/v1",
        )
        == 32768
    )
    assert calls == [("GET", "http://vllm.test/v1/models", None)]


def test_ollama_show_payload_context_window(monkeypatch):
    calls = []

    def fake_request_json(method: str, url: str, *, json_body=None):
        calls.append((method, url, json_body))
        return {
            "model_info": {
                "general.architecture": "llama",
                "llama.context_length": 8192,
            }
        }

    monkeypatch.setattr(model_metadata, "_request_json", fake_request_json)

    assert (
        model_metadata.probe_local_context_window(
            "llama3.2",
            "ollama",
            base_url="http://ollama.test/v1",
        )
        == 8192
    )
    assert calls == [("POST", "http://ollama.test/api/show", {"model": "llama3.2"})]


def test_llamacpp_props_payload_context_window(monkeypatch):
    calls = []

    def fake_request_json(method: str, url: str, *, json_body=None):
        calls.append((method, url, json_body))
        return {"default_generation_settings": {"n_ctx": 65536}}

    monkeypatch.setattr(model_metadata, "_request_json", fake_request_json)

    assert (
        model_metadata.probe_local_context_window(
            "ggml-org/model:Q4_K_M",
            "llama.cpp",
            base_url="http://llamacpp.test/v1",
        )
        == 65536
    )
    assert calls == [
        (
            "GET",
            "http://llamacpp.test/props?model=ggml-org%2Fmodel%3AQ4_K_M",
            None,
        )
    ]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.TimeoutException("timed out"),
        httpx.ConnectError("connection failed"),
        "garbage-json",
    ],
)
def test_probe_failure_returns_none_and_runtime_falls_back(monkeypatch, failure):
    def fake_request_json(_method: str, _url: str, *, json_body=None):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(model_metadata, "_request_json", fake_request_json)
    monkeypatch.setattr(model_metadata, "context_window", lambda _model, _provider: 12345)

    assert (
        model_metadata.probe_local_context_window(
            "served-model",
            "openai-completions",
            base_url="http://vllm.test/v1",
        )
        is None
    )
    assert runtime._resolve_context_window(
        "served-model",
        "openai-completions",
        "http://vllm.test/v1",
    ) == 12345


def test_non_local_provider_is_not_probed(monkeypatch):
    calls = []

    def fake_request_json(_method: str, _url: str, *, json_body=None):
        calls.append((_method, _url, json_body))
        raise AssertionError("non-local providers must not be probed")

    monkeypatch.setattr(model_metadata, "_request_json", fake_request_json)
    monkeypatch.setattr(model_metadata, "context_window", lambda _model, _provider: 777)

    assert (
        model_metadata.probe_local_context_window(
            "gpt-4.1",
            "openai",
            base_url="http://localhost:9999/v1",
        )
        is None
    )
    assert runtime._resolve_context_window("gpt-4.1", "openai", "http://localhost:9999/v1") == 777
    assert calls == []


def test_probe_cache_avoids_second_query(monkeypatch):
    calls = []

    def fake_request_json(method: str, url: str, *, json_body=None):
        calls.append((method, url, json_body))
        return {"data": [{"id": "served-model", "context_window": 16384}]}

    monkeypatch.setattr(model_metadata, "_request_json", fake_request_json)

    for _ in range(2):
        assert (
            model_metadata.probe_local_context_window(
                "served-model",
                "openai-completions",
                base_url="http://vllm.test/v1",
            )
            == 16384
        )

    assert calls == [("GET", "http://vllm.test/v1/models", None)]
