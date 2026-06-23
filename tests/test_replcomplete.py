"""Tab-completion behavior: prefix match (never fuzzy), context routing."""

from __future__ import annotations

from js.replcomplete import JsCompleter, command_candidates, path_candidates


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
