"""Tab-completion behavior: prefix match (never fuzzy), context routing."""

from __future__ import annotations

import pytest

from js import cli
from js.replcomplete import JsCompleter, command_candidates, path_candidates, value_candidates


def _table():
    return cli._command_completions({})


# ---- command context (first word) ----

def test_command_prefix_match_not_fuzzy():
    assert "/compact" in command_candidates("/comp", _table())
    assert "/compact" in command_candidates("comp", _table())   # bare word -> implicit slash
    assert command_candidates("cpt", _table()) == []            # subsequence must NOT match


def test_every_table_command_completes():
    assert command_candidates("/", _table()) == sorted("/" + verb for verb in cli.COMMANDS)


def test_bare_quit_words_complete():
    completer = JsCompleter(commands=_table, bare_words=cli.QUIT_WORDS)

    assert completer.candidates("ex")[0] == ["exit"]
    assert {"quit", "/quit"} <= set(completer.candidates("qu")[0])
    for word in cli.QUIT_WORDS:
        assert word in completer.candidates(word)[0]


def test_a_new_table_entry_completes_with_no_completer_change(monkeypatch):
    command = cli.Command(lambda arg, state, cfg: None, "frobnicate", "test entry")
    monkeypatch.setitem(cli.COMMANDS, "frobnicate", command)
    completer = JsCompleter(commands=lambda: cli._command_completions({}))

    assert completer.candidates("/frob")[0] == ["/frobnicate"]


def test_aliases_complete_as_commands():
    completer = JsCompleter(commands=lambda: cli._command_completions({"aliases": {"ship": "save"}}))

    assert completer.candidates("/sh")[0] == ["/ship", "/show"]


def test_shared_prefix_yields_all_for_rotation():
    cands = command_candidates("/re", _table())
    assert {"/reset", "/refresh-model-catalog"} <= set(cands)


def test_compact_prefix_rotates_compact_and_compact_auto():
    # /compac is a prefix of both -> Tab rotates between them
    assert set(command_candidates("/compac", _table())) == {"/compact", "/compact-auto"}


# ---- routing through JsCompleter.candidates ----

def _completer(spell=lambda w: ["the"] if w == "teh" else []):
    return JsCompleter(
        commands=_table,
        setting_keys=["compact.auto", "compact.model", "model.id", "model.reasoning_effort"],
        names=lambda: ["deepseek", "openai", "myvllm"],
        spell=spell,
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
    always_spell = _completer(spell=lambda _w: ["SHOULD_NOT_APPEAR"])
    cands, _ = always_spell.candidates(f"{cmd} qwen")
    assert cands == []


def test_unknown_command_prose_still_reaches_spellchecker():
    always_spell = _completer(spell=lambda _w: ["SHOULD_APPEAR"])
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
