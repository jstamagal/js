"""Tab-completion behavior: prefix match (never fuzzy), context routing."""

from __future__ import annotations

import pytest

from js import cli
from js.replcomplete import JsCompleter, command_candidates, path_candidates, value_candidates


def test_completer_uses_supplied_command_prefix():
    completer = JsCompleter(commands=lambda: {"set": "keys"}, setting_keys=("MODEL",), command_prefix=":")
    assert completer.candidates(":se") == ([":set"], 3)
    assert completer.candidates(":set MO") == (["MODEL"], 2)


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

    assert "exit" in completer.candidates("ex")[0]
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
        setting_keys=["COMPACTION_AUTO", "COMPACTION_EXTERNAL_MODEL", "MODEL", "EFFORT"],
        names=lambda: ["deepseek", "openai", "myvllm"],
        spell=spell,
    )


def test_set_arg_completes_keys():
    cands, n = _completer().candidates("/set COMPACTION_")
    assert cands == ["COMPACTION_AUTO", "COMPACTION_EXTERNAL_MODEL"]
    assert n == len("COMPACTION_")


def test_show_arg_completes_keys():
    cands, _ = _completer().candidates("/show MO")
    assert cands == ["MODEL"]


def test_value_candidates_prefix_matches_reasoning_effort_stops():
    assert value_candidates("EFFORT", "m") == ["max", "medium", "minimal"]
    assert value_candidates("EFFORT", "zzz") == []


def test_value_candidates_empty_for_unknown_key():
    assert value_candidates("MODEL", "de") == []


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


def test_path_completion_treats_filename_metacharacters_as_literal_prefixes(tmp_path):
    literal = tmp_path / "report[1].txt"
    literal.write_text("wanted", encoding="utf-8")
    (tmp_path / "report1.txt").write_text("other file", encoding="utf-8")
    token = "@" + str(tmp_path / "report[1]")

    candidates, _ = _completer().candidates(f"read {token}")

    assert candidates == ["@" + str(literal)]


def test_registered_spellcheck_language_reaches_hunspell(monkeypatch):
    from types import SimpleNamespace
    from js import fset, replcomplete

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout="*\n")

    monkeypatch.setattr(replcomplete.subprocess, "run", run)
    fset.bind_settings({"SPELLCHECK_LANGUAGE": "de_DE", "SPELLCHECK_TIMEOUT": 7})
    replcomplete.hunspell_suggest("wort")
    assert calls[0][0][-1] == "de_DE"
    assert calls[0][1]["timeout"] == 7


def test_completion_routes_and_enums_come_from_the_caller():
    completer = JsCompleter(commands=lambda: {"set": "set"},
                            path_completion=lambda token: None,
                            unknown_arguments=lambda head, token: [head + ":" + token],
                            value_enums={"CUSTOM": ("chosen",)})
    assert completer.candidates("/set CUSTOM ch") == (["chosen"], 2)
    assert completer.candidates("hello ./file") == (["hello:./file"], 6)
