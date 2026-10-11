"""model_matching: which catalog row answers a routed model name
(ALL-CLAIMS model_matching.py HIGH x4 + MED dates/window; design ziz #295/#512,
CALLs ruhondo #274/#530/#648)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from js import model_matching as M


def row(provider, model, family, date, window):
    return SimpleNamespace(provider_id=provider, model_id=model, family=family,
                           release_date=date, context_window=window)


CASES = [
    # A maker's real `-latest` id keeps its own limits over a newer maker sibling
    # and a reseller relist (ALL-CLAIMS:328).
    ("gpt-5.2-chat-latest", [
        row("openai", "gpt-5.2-chat-latest", "gpt-5.2-chat", "2025-12-11", 128000),
        row("openai", "gpt-5.2-chat", "gpt-5.2-chat", "2026-02-01", 400000),
        row("abacus", "gpt-5.2-chat-latest", "gpt-5.2-chat-latest", "2026-01-01", 400000),
    ], ("openai", "gpt-5.2-chat-latest", 128000), "normalized"),
    # A reseller alias row with a refresh date never wins the family (:329, :327).
    ("claude-opus-latest", [
        row("edenai", "anthropic/claude-opus-latest", "claude-opus-latest", "2026-09-22", 1000000),
        row("anthropic", "claude-opus-5", "claude-opus", "2026-07-24", 1000000),
        row("anthropic", "claude-opus-4-1", "claude-opus", "2025-08-01", 200000),
    ], ("anthropic", "claude-opus-5", 1000000), "family-release"),
    ("opus-latest", [
        row("edenai", "anthropic/claude-opus-latest", "claude-opus-latest", "2026-09-22", 1000000),
        row("anthropic", "claude-opus-5", "claude-opus", "2026-07-24", 1000000),
    ], ("anthropic", "claude-opus-5", 1000000), "family-release"),
    # The maker beats a reseller with a later date (:327).
    ("gpt-5-mini-latest", [
        row("jiekou", "gpt-5-mini", "gpt-5-mini", "2026-01-01", 400000),
        row("openai", "gpt-5-mini", "gpt-5-mini", "2025-11-01", 128000),
    ], ("openai", "gpt-5-mini", 128000), "family-release"),
    # Resellers only, three windows: no majority -> the smallest, either date order (:332).
    ("gpt-5.1-codex-max", [
        row("abacus", "gpt-5.1-codex-max", "gpt-5.1-codex", "2026-03-01", 400000),
        row("edenai", "gpt-5.1-codex-max", "gpt-5.1-codex", "2026-02-01", 272000),
        row("ofox", "gpt-5.1-codex-max", "gpt-5.1-codex", "2026-01-01", 256000),
    ], ("ofox", "gpt-5.1-codex-max", 256000), "normalized"),
    ("gpt-5.1-codex-max", [
        row("abacus", "gpt-5.1-codex-max", "gpt-5.1-codex", "2026-01-01", 400000),
        row("edenai", "gpt-5.1-codex-max", "gpt-5.1-codex", "2026-02-01", 272000),
        row("ofox", "gpt-5.1-codex-max", "gpt-5.1-codex", "2026-03-01", 256000),
    ], ("ofox", "gpt-5.1-codex-max", 256000), "normalized"),
    # A literal `-max` row answers for itself (:331 boundary).
    ("kimi-k2-max", [
        row("moonshotai", "kimi-k2-max", "kimi-k2", "2026-01-01", 131072),
        row("moonshotai", "kimi-k2", "kimi-k2", "2025-12-01", 131072),
    ], ("moonshotai", "kimi-k2-max", 131072), "normalized"),
    ("claude-opus-4-high", [
        row("anthropic", "claude-opus-4", "claude-opus", "2025-05-01", 200000),
        row("resellerx", "claude-opus-4", "claude-opus", "2026-01-01", 250000),
    ], ("anthropic", "claude-opus-4", 200000), "effort"),
    # Two resellers copying one number never outvote the maker (#530).
    ("gpt-7", [
        row("openai", "gpt-7", "gpt-7", "2026-01-01", 128000),
        row("r1", "gpt-7", "gpt-7", "2026-02-01", 400000),
        row("r2", "gpt-7", "gpt-7", "2026-03-01", 400000),
    ], ("openai", "gpt-7", 128000), "normalized"),
    ("gpt-8", [
        row("openai", "gpt-8", "gpt-8", "2026-01-01", 128000),
        row("r1", "gpt-8", "gpt-8", "2026-02-01", 64000),
    ], ("openai", "gpt-8", 128000), "normalized"),
    # No window is no vote: a stated window beats None (#648; a None window
    # means compaction never fires, context_budget.py:397-407).
    ("gpt-none-1", [
        row("openai", "gpt-none-1", "gpt-none-1", "2026-01-01", None),
        row("r1", "gpt-none-1", "gpt-none-1", "2026-02-01", 400000),
    ], ("r1", "gpt-none-1", 400000), "normalized"),
    ("gpt-none-2", [
        row("openai", "gpt-none-2", "gpt-none-2", "2026-01-01", 128000),
        row("r1", "gpt-none-2", "gpt-none-2", "2026-02-01", None),
    ], ("openai", "gpt-none-2", 128000), "normalized"),
    ("gpt-none-3", [
        row("a", "gpt-none-3", "gpt-none-3", "2026-01-01", None),
        row("b", "gpt-none-3", "gpt-none-3", "2026-02-01", None),
    ], ("b", "gpt-none-3", None), "normalized"),
    # POLICY, NOT TRUTH: with no maker row the majority of resellers wins; two
    # copying one wrong number outvote the one right one.
    ("gpt-9", [
        row("a", "gpt-9", "gpt-9", "2026-01-01", 400000),
        row("b", "gpt-9", "gpt-9", "2026-02-01", 400000),
        row("c", "gpt-9", "gpt-9", "2026-03-01", 256000),
    ], ("b", "gpt-9", 400000), "normalized"),
    # Duplicate maker rows that disagree: the smaller (doc #540).
    ("gpt-10", [
        row("openai", "gpt-10", "gpt-10", "2026-01-01", 128000),
        row("openai", "gpt-10", "gpt-10", "2026-01-01", 400000),
        row("r1", "gpt-10", "gpt-10", "2026-02-01", 400000),
        row("r2", "gpt-10", "gpt-10", "2026-02-01", 400000),
    ], ("openai", "gpt-10", 128000), "normalized"),
]


@pytest.mark.parametrize("request_name,rows,expected,method", CASES, ids=[c[0] for c in CASES])
def test_match_routed(request_name, rows, expected, method):
    found, how = M.match_routed(request_name, tuple(rows))
    assert (found.provider_id, found.model_id, found.context_window) == expected
    assert how == method


def test_name_key_folds_only_a_trailing_latest_tag():
    """ALL-CLAIMS:326: `cpa/opus:latest` resolved nothing."""
    assert M.name_key("cpa/opus:latest") == "opus-latest"
    assert M.name_key("OPUS:LATEST") == "opus-latest"
    assert M.name_key("openai/gpt-5.2:0731") == "gpt-5-2:0731"
    assert M.name_key("x:free") == "x:free"


def test_latest_in_the_middle_of_a_name_is_an_exact_name():
    rows = (row("openai", "gpt-latest-preview", "gpt", "2026-01-01", 100000),
            row("openai", "gpt-6", "gpt", "2026-05-01", 400000))
    found, how = M.match_routed("gpt-latest-preview", rows)
    assert (found.model_id, how) == ("gpt-latest-preview", "normalized")


@pytest.mark.parametrize("older,newer", [("2025-12-31", "2026-01"), ("2026-01", "2026-01-15"), ("2025", "2025-02")])
def test_release_dates_of_mixed_precision_compare_by_calendar(older, newer):
    """ALL-CLAIMS:330: '2026-01' > '2025-12-31' only by accident of string order."""
    rows = (row("anthropic", "claude-x-1", "claude-x", older, 100000),
            row("anthropic", "claude-x-2", "claude-x", newer, 200000))
    found, _ = M.match_routed("claude-x-latest", rows)
    assert found.model_id == "claude-x-2"


def test_issuer_table_knows_openai_o_series_and_meta():
    assert M.issuer_rank(row("openai", "o3-mini", None, "2026-01-01", 1)) == 1
    assert M.issuer_rank(row("router", "o4-mini", None, "2026-01-01", 1)) == 0
    assert M.issuer_rank(row("meta", "llama-4", None, "2026-01-01", 1)) == 1
