"""Tab-completion behavior: prefix match (never fuzzy), context routing."""

from __future__ import annotations

import pytest

from js.replcomplete import JsCompleter, command_candidates, path_candidates, value_candidates


# ---- command context (first word) ----

def test_command_prefix_match_not_fuzzy():
    assert "/compact" in command_candidates("/comp")
    assert "/compact" in command_candidates("comp")        # bare word -> implicit slash
    assert command_candidates("cpt") == []                 # subsequence must NOT match


def test_slash_lists_slash_commands_only():
    cands = command_candidates("/")
    assert "/set" in cands and "/compact" in cands
    assert "exit" not in cands                              # exit has no leading slash


def test_nonslash_commands_complete():
    assert command_candidates("ex") == ["exit"]
    assert "quit" in command_candidates("qu")
    assert "/load" in command_candidates("lo")
    assert "/on" in command_candidates("o")


def test_shared_prefix_yields_all_for_rotation():
    cands = command_candidates("/re")
    assert {"/reset", "/refresh-model-catalog"} <= set(cands)


def test_compact_prefix_rotates_compact_and_compact_auto():
    # /compac is a prefix of both -> Tab rotates between them
    assert set(command_candidates("/compac")) == {"/compact", "/compact-auto"}


# ---- routing through JsCompleter.candidates ----

def _completer():
    return JsCompleter(
        setting_keys=["compact.auto", "compact.model", "model.id", "model.reasoning_effort"],
        names=lambda: ["deepseek", "openai", "myvllm"],
        spell=lambda w: ["the"] if w == "teh" else [],
    )


def test_set_arg_completes_keys():
    cands, n = _completer().candidates("/set compact.")
    assert cands == ["compact.auto", "compact.model"]
    assert n == len("compact.")


def test_show_arg_completes_keys():
    cands, _ = _completer().candidates("/show model.")
    assert cands == ["model.id", "model.reasoning_effort"]


def test_value_candidates_prefix_matches_reasoning_effort_stops():
    assert value_candidates("model.reasoning_effort", "m") == ["max", "medium", "minimal"]
    assert value_candidates("model.reasoning_effort", "zzz") == []


def test_value_candidates_empty_for_unknown_key():
    assert value_candidates("model.id", "de") == []


def test_set_reasoning_effort_value_completes_xhigh():
    # FINDING 54: `/set model.reasoning_effort x<tab>` must offer `xhigh`.
    cands, n = _completer().candidates("/set model.reasoning_effort x")
    assert cands == ["xhigh"]
    assert n == len("x")


def test_set_reasoning_effort_value_lists_all_stops_when_empty():
    cands, _ = _completer().candidates("/set model.reasoning_effort ")
    assert cands == ["high", "low", "max", "medium", "minimal", "off", "xhigh"]


def test_set_non_enum_key_value_has_no_candidates():
    cands, _ = _completer().candidates("/set model.id de")
    assert cands == []


def test_show_second_word_still_completes_keys_not_values():
    # /show never takes a value, so a second word still completes knob keys
    # (not the reasoning-effort enum), unlike /set.
    cands, _ = _completer().candidates("/show model.reasoning_effort m")
    assert cands == ["model.id", "model.reasoning_effort"]


def test_login_arg_completes_names():
    cands, _ = _completer().candidates("/login dee")
    assert cands == ["deepseek"]


def test_on_arg_completes_event_names():
    cands, _ = _completer().candidates("/on tool_")
    assert cands == ["tool_call", "tool_result"]


def test_provider_arg_completes_names():
    cands, _ = _completer().candidates("/provider my")
    assert cands == ["myvllm"]


def test_midline_word_routes_to_spell():
    cands, n = _completer().candidates("fix teh")
    assert cands == ["the"]
    assert n == len("teh")


@pytest.mark.parametrize("cmd", ["/model", "/baseurl", "/apikey", "/models", "/compact"])
def test_known_command_args_never_reach_spellchecker(cmd):
    # A model id like "qwen" would otherwise get English spelling suggestions
    # ("wen", "Owen", "Gwen", ...) that silently replace it on Tab.
    always_spell = JsCompleter(spell=lambda _w: ["SHOULD_NOT_APPEAR"])
    cands, _ = always_spell.candidates(f"{cmd} qwen")
    assert cands == []


def test_unknown_command_prose_still_reaches_spellchecker():
    always_spell = JsCompleter(spell=lambda _w: ["SHOULD_APPEAR"])
    cands, _ = always_spell.candidates("fix teh")
    assert cands == ["SHOULD_APPEAR"]


def test_path_like_token_routes_to_filesystem(tmp_path):
    (tmp_path / "alpha.txt").write_text("x", encoding="utf-8")
    (tmp_path / "beta.txt").write_text("x", encoding="utf-8")
    token = str(tmp_path / "al")
    cands, _ = _completer().candidates(f"read {token}")
    assert cands == [str(tmp_path / "alpha.txt")]


def test_at_path_preserves_at_prefix(tmp_path):
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    token = "@" + str(tmp_path / "no")
    cands, _ = _completer().candidates(f"summarize {token}")
    assert cands == ["@" + str(tmp_path / "notes.md")]


def test_path_candidates_marks_directories(tmp_path):
    (tmp_path / "sub").mkdir()
    cands = path_candidates(str(tmp_path / "su"))
    assert cands == [str(tmp_path / "sub") + "/"]
