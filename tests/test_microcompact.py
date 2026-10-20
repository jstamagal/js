from __future__ import annotations

import ai

from js import compaction, runtime


def _history(n_tools: int, body_chars: int = 5_000) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "sys"}]
    for i in range(n_tools):
        msgs.append({"role": "assistant", "content": f"thinking {i}"})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "read",
                     "content": "x" * body_chars})
    msgs.append({"role": "user", "content": "now what"})
    return msgs


def test_microcompact_clears_old_tool_bodies_and_keeps_recent_ones():
    msgs = _history(30)
    cleared, reclaimed = compaction.microcompact(msgs, keep_recent=20)

    assert cleared == 10
    assert reclaimed > 40_000
    tools = [m for m in msgs if m["role"] == "tool"]
    assert all(t["content"] == compaction.MICROCOMPACT_CLEARED_MESSAGE for t in tools[:10])
    assert all(t["content"] != compaction.MICROCOMPACT_CLEARED_MESSAGE for t in tools[10:])
    # Assistant reasoning about those results is untouched.
    assert all(m["content"].startswith("thinking") for m in msgs if m["role"] == "assistant")


def test_microcompact_leaves_small_results_alone():
    msgs = _history(30, body_chars=10)
    cleared, reclaimed = compaction.microcompact(msgs, keep_recent=0, min_chars=400)
    assert (cleared, reclaimed) == (0, 0)


def test_microcompact_is_idempotent():
    msgs = _history(30)
    first = compaction.microcompact(msgs, keep_recent=0)
    second = compaction.microcompact(msgs, keep_recent=0)
    assert first[0] == 30
    assert second == (0, 0)


def test_microcompact_preserves_tool_call_ids_so_the_history_stays_valid():
    # A tool message orphaned from its call id breaks the next request.
    msgs = _history(5)
    compaction.microcompact(msgs, keep_recent=0)
    tools = [m for m in msgs if m["role"] == "tool"]
    assert [t["tool_call_id"] for t in tools] == [f"c{i}" for i in range(5)]
    assert all(t["name"] == "read" for t in tools)


def test_microcompact_keep_recent_larger_than_history_clears_nothing():
    msgs = _history(3)
    assert compaction.microcompact(msgs, keep_recent=20) == (0, 0)


def test_spill_writes_the_full_result_and_returns_a_pointer(tmp_path):
    big = "y" * 200_000
    out = runtime.spill_oversized_result(big, 50_000, spill_dir=tmp_path)

    assert len(out) < len(big)
    assert "TOOL_RESULT_INLINE_MAX" in out
    written = list(tmp_path.glob("result-*.txt"))
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == big
    assert str(written[0]) in out


def test_spill_leaves_small_results_alone_and_can_be_disabled(tmp_path):
    small = "z" * 100
    assert runtime.spill_oversized_result(small, 50_000, spill_dir=tmp_path) == small
    big = "z" * 200_000
    assert runtime.spill_oversized_result(big, 0, spill_dir=tmp_path) == big
    assert list(tmp_path.glob("result-*.txt")) == []


def test_post_compact_rehydration_reattaches_recent_files(tmp_path):
    a = tmp_path / "a.py"
    a.write_text("print('a')\n", encoding="utf-8")
    b = tmp_path / "b.py"
    b.write_text("print('b')\n", encoding="utf-8")

    class Ctx:
        read_paths = {a, b}

    msg = compaction._post_compact_rehydration(Ctx())
    assert msg["role"] == "user"
    assert "print('a')" in msg["content"]
    assert "print('b')" in msg["content"]
    assert "post-compaction-files" in msg["content"]


def test_post_compact_rehydration_names_but_skips_huge_files(tmp_path):
    big = tmp_path / "big.log"
    big.write_text("q" * 500_000, encoding="utf-8")

    class Ctx:
        read_paths = {big}

    msg = compaction._post_compact_rehydration(Ctx(), per_file_tokens=1_000)
    assert "too large to re-attach" in msg["content"]
    assert "qqqq" not in msg["content"]


def test_post_compact_rehydration_is_none_without_reads():
    class Ctx:
        read_paths = set()

    assert compaction._post_compact_rehydration(Ctx()) is None


# Overflow classification: recovery, microcompaction and the manual /compact path
# all hang off is_context_overflow_error, so a provider whose phrasing misses
# every needle gets no recovery at all — which is what happened on llama.cpp, the
# daily driver, until 4bb7562 added the needles. That fix shipped without a test.
LLAMACPP_OVERFLOW_MESSAGE = (
    "Error code: 400 - {'error': {'code': 400, 'message': 'request (97720 tokens) "
    "exceeds the available context size (89600 tokens), try increasing it', "
    "'type': 'exceed_context_size_error'}}"
)


def test_llamacpp_overflow_wording_is_classified_as_context_overflow():
    exc = ai.ProviderAPIError(
        LLAMACPP_OVERFLOW_MESSAGE,
        provider="openai",
        error_type="exceed_context_size_error",
    )

    assert compaction.is_context_overflow_error(exc) is True


def test_overflow_classification_does_not_fire_on_unrelated_provider_errors():
    for message in (
        "Error code: 401 - invalid api key",
        "Error code: 400 - unknown model 'gpt-9'",
        "Error code: 500 - internal server error",
    ):
        assert compaction.is_context_overflow_error(ai.ProviderAPIError(message)) is False


def test_overflow_classification_ignores_non_provider_exceptions():
    assert compaction.is_context_overflow_error(RuntimeError("context length exceeded")) is False


def test_recovery_records_failure_with_original_tool_bodies(monkeypatch, tmp_path, capsys):
    import json
    import pytest
    from test_config_compaction_layers import _compact_test_cfg

    cfg = _compact_test_cfg(tmp_path, {"COMPACTION_LOG_DIR": str(tmp_path / "flights")})
    msgs = _history(22)
    original = list(msgs)

    def fail(*args, **kwargs):
        raise RuntimeError("clear failed")

    monkeypatch.setattr(compaction, "microcompact", fail)
    with pytest.raises(RuntimeError, match="clear failed"):
        compaction.recover_overflow(msgs, 1, cfg=cfg, system="SYSTEM",
                                    error=ai.ProviderAPIError("context length exceeded"), flight_data={},
                                    log_dir=compaction.flight_log_dir(cfg), emit=compaction.printing())
    records = [json.loads(line) for line in next((tmp_path / "flights").glob("*.jsonl")).read_text().splitlines()]
    assert records[1]["messages"] == original
    assert records[-2]["messages"] == original
    assert records[-1]["event"] == "failure"
    assert records[0]["id"][:12] in capsys.readouterr().err


def test_recovery_records_no_eligible_results(tmp_path, capsys):
    import json
    from test_config_compaction_layers import _compact_test_cfg

    cfg = _compact_test_cfg(tmp_path, {"COMPACTION_LOG_DIR": str(tmp_path / "flights")})
    msgs = _history(2)
    assert compaction.recover_overflow(msgs, 1, cfg=cfg, system="SYSTEM",
                                      error=ai.ProviderAPIError("context length exceeded"), flight_data={},
                                      log_dir=compaction.flight_log_dir(cfg),
                                      emit=compaction.printing()) == ("summarize", 0, 0)
    records = [json.loads(line) for line in next((tmp_path / "flights").glob("*.jsonl")).read_text().splitlines()]
    assert records[-1]["event"] == "skipped"
    assert records[1]["messages"] == records[-2]["messages"]
    assert records[0]["id"][:12] in capsys.readouterr().err


def _status_error(message, status, **kwargs):
    return ai.ProviderAPIError(message, http_context=ai.HTTPErrorContext(status_code=status), **kwargs)


def test_rate_limit_with_413_in_request_id_is_not_overflow():
    """ALL-CLAIMS:136 — "413" inside req_84133 made a 429 look like an overflow."""
    exc = _status_error(
        "Rate limit reached for gpt-x. Limit 2000000, Used 1999000. request id req_84133",
        429, code="rate_limit_exceeded",
    )
    assert compaction.is_context_overflow_error(exc) is False


def test_openai_tpm_request_too_large_is_not_overflow():
    message = ("Request too large for gpt-4o on tokens per min (TPM): Limit 30000, "
               "Requested 41330. The input or output tokens must be reduced.")
    assert compaction.is_context_overflow_error(_status_error(message, 429, code="rate_limit_exceeded")) is False
    # Same text with the status lost still reads as a rate limit.
    assert compaction.is_context_overflow_error(ai.ProviderAPIError(message)) is False
    assert compaction.is_context_overflow_error(ai.ProviderRateLimitError(message)) is False


def test_413_in_unrelated_numbers_is_not_overflow():
    for message in (
        "Error code: 400 - unknown model 'gpt-9' (request req_84133)",
        "Error code: 500 - upstream 10.0.0.1:4130 refused",
        "Error code: 401 - key sk-4131 invalid",
    ):
        assert compaction.is_context_overflow_error(ai.ProviderAPIError(message)) is False, message


def test_real_overflow_shapes_still_classify():
    cases = [
        _status_error("Payload Too Large", 413),
        ai.ProviderRequestTooLargeError("request_too_large"),
        ai.ProviderAPIError("Error code: 413 - request entity too large"),
        _status_error("prompt is too long: 210000 tokens > 200000 maximum", 400,
                      error_type="invalid_request_error"),
        _status_error("This model's maximum context length is 128000 tokens.", 400,
                      code="context_length_exceeded"),
        _status_error("The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).", 400),
        ai.ProviderAPIError("x", code="exceed_context_size_error"),
    ]
    for exc in cases:
        assert compaction.is_context_overflow_error(exc) is True, exc
