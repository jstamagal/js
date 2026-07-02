"""Tests for {{VAR}} / !{sub ...} / ```!sub inline-directive expansion."""

from __future__ import annotations

import shutil

import pytest

from js.promptexpand import expand_prompt, PromptExpansionError


# ---- {{VAR}} env shorthand (always on) ----

def test_env_shorthand_expands():
    assert expand_prompt("ctx: {{FOO}}", env={"FOO": "bar"}) == "ctx: bar"


def test_unset_env_shorthand_is_empty_string():
    assert expand_prompt("a{{MISSING}}b", env={}) == "ab"


def test_unrecognized_braces_left_literal():
    text = "use {{double word}} as a marker"
    assert expand_prompt(text, env={}) == text


def test_no_directives_returned_unchanged():
    text = "plain prompt with $ and # and no directives"
    assert expand_prompt(text, env={}) == text


def test_single_pass_no_recursion():
    # an expanded value containing another directive is NOT re-scanned
    assert expand_prompt("{{A}}", env={"A": "{{B}}", "B": "leak"}) == "{{B}}"


# ---- !{env ...} / !{file ...} (read-only subsystems, ungated) ----

def test_inline_env_subsystem():
    assert expand_prompt("v=!{env WHO}", env={"WHO": "ape"}) == "v=ape"


def test_inline_file_subsystem(tmp_path):
    f = tmp_path / "ctx.txt"
    f.write_text("hello from file", encoding="utf-8")
    assert expand_prompt(f"data: !{{file {f}}}", env={}) == "data: hello from file"


def test_file_missing_skips():
    # A missing/unreadable file embeds nothing instead of aborting the prompt.
    assert expand_prompt("a!{file /no/such/path/xyz}b", env={}) == "ab"


def test_unknown_subsystem_errors():
    with pytest.raises(PromptExpansionError) as e:
        expand_prompt("!{wat hi}", env={})
    assert "unknown inline subsystem" in str(e.value)


# ---- backtick-quoted directives stay literal (documentation, not a request) ----

def test_backtick_wrapped_code_directive_not_evaluated():
    # the bug: a `!{sh ...}` example in a prompt tripped the code gate at load.
    text = "Executable directives -- `!{sh ...}`, `!{bash ...}` -- use sparingly."
    assert expand_prompt(text, allow_code=False) == text


def test_backtick_wrapped_readonly_directive_not_blanked():
    # `!{file PATH}` / `{{ENV}}` examples must survive, not expand to "".
    text = "Inject with `!{file PATH}` or `{{ENV_NAME}}` in the prompt."
    assert expand_prompt(text, env={}) == text


def test_unwrapped_directive_still_expands_next_to_backticks():
    # a stray leading backtick must NOT suppress a real directive.
    assert expand_prompt("see `code`: !{env WHO}", env={"WHO": "ape"}) == "see `code`: ape"


def test_lone_leading_backtick_does_not_suppress():
    assert expand_prompt("`!{env WHO}", env={"WHO": "ape"}) == "`ape"


# ---- backslash escapes ANY form (the only escape for fenced blocks) ----

def test_backslash_escapes_inline_code_directive():
    assert expand_prompt(r"run \!{sh echo hi}", allow_code=False) == "run !{sh echo hi}"


def test_backslash_escapes_env_shorthand():
    assert expand_prompt(r"lit \{{FOO}}", env={"FOO": "bar"}) == "lit {{FOO}}"


def test_backslash_escapes_fenced_block():
    text = "show:\n\\```!sh\necho hi\n```\n"
    assert expand_prompt(text, allow_code=False) == "show:\n```!sh\necho hi\n```\n"


def test_unescaped_directive_after_escaped_one_still_runs():
    out = expand_prompt(r"\!{env WHO} vs !{env WHO}", env={"WHO": "ape"})
    assert out == "!{env WHO} vs ape"


# ---- code subsystems are gated ----

def test_sh_gated_off_errors():
    with pytest.raises(PromptExpansionError) as e:
        expand_prompt("!{sh echo hi}", allow_code=False)
    assert "--dangerously-evaluate-inline-code" in str(e.value)


def test_sh_inline_runs_when_allowed():
    assert expand_prompt("!{sh printf hello}", allow_code=True) == "hello"


def test_sh_nonzero_exit_errors():
    with pytest.raises(PromptExpansionError) as e:
        expand_prompt("!{sh sh -c 'echo boom >&2; exit 4'}", allow_code=True)
    assert "4" in str(e.value) and "boom" in str(e.value)


# ---- fenced ```!sub blocks ----

def test_fence_sh_block():
    text = "out:\n```!sh\nprintf one\nprintf two\n```\n"
    assert expand_prompt(text, allow_code=True) == "out:\nonetwo\n"


def test_fence_gated_off_errors():
    text = "```!sh\necho hi\n```"
    with pytest.raises(PromptExpansionError):
        expand_prompt(text, allow_code=False)


@pytest.mark.skipif(not shutil.which("python3"), reason="python3 not on PATH")
def test_fence_python_block():
    text = "```!python\nprint(6 * 7)\n```"
    assert expand_prompt(text, allow_code=True) == "42"


@pytest.mark.skipif(not shutil.which("python3"), reason="python3 not on PATH")
def test_inline_python_runs_in_invocation_cwd(tmp_path, monkeypatch):
    # An env-probing directive must see the dir js was launched from, not the
    # throwaway temp dir the snippet is compiled in.
    monkeypatch.chdir(tmp_path)
    out = expand_prompt("cwd=!{python import os; print(os.getcwd())}", allow_code=True)
    assert out == f"cwd={tmp_path.resolve()}"


@pytest.mark.skipif(not shutil.which("python3"), reason="python3 not on PATH")
def test_inline_python_snippet_does_not_litter_cwd(tmp_path, monkeypatch):
    # Running in the invocation cwd must not leave the snippet file behind there.
    monkeypatch.chdir(tmp_path)
    expand_prompt("!{python print(1)}", allow_code=True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not (shutil.which("cc") or shutil.which("gcc")), reason="no C compiler")
def test_fence_c_block_compiles_runs_injects_stdout():
    text = '```!c\n#include <stdio.h>\nint main(void){ printf("answer=%d", 6*7); return 0; }\n```'
    assert expand_prompt(text, allow_code=True) == "answer=42"


# ---- persona integration: cfg.allow_inline_code drives the gate ----

def _cfg(prompts_dir, *, allow_inline_code=False):
    class Cfg:
        pass
    c = Cfg()
    c.prompt_roots = ()
    c.agents_files = ()
    c.allow_inline_code = allow_inline_code
    c.prompts_dir = prompts_dir
    return c


def test_persona_expands_env(tmp_path, monkeypatch):
    import js.persona as P
    d = tmp_path / "agent"
    d.mkdir()
    (d / "00-seed.md").write_text("hello {{NAME}}\n", encoding="utf-8")
    monkeypatch.setenv("NAME", "world")
    spec = P.load_configured_prompt_spec(_cfg(d))
    assert "hello world" in spec.system


def test_persona_code_blocked_without_flag(tmp_path):
    import js.persona as P
    d = tmp_path / "agent"
    d.mkdir()
    (d / "00-seed.md").write_text("ctx !{sh echo hi}\n", encoding="utf-8")
    with pytest.raises(PromptExpansionError):
        P.load_configured_prompt_spec(_cfg(d, allow_inline_code=False))


def test_default_inline_code_timeout_is_300():
    import js.promptexpand as P
    assert P._DEFAULT_TIMEOUT_S == 300


def test_persona_passes_configured_inline_code_timeout(tmp_path, monkeypatch):
    import js.persona as P

    seen = {}

    def fake_expand(text, *, allow_code=False, env=None, timeout_s=None):
        seen["text"] = text
        seen["allow_code"] = allow_code
        seen["timeout_s"] = timeout_s
        return text + " expanded"

    d = tmp_path / "agent"
    d.mkdir()
    (d / "00-seed.md").write_text("ctx !{sh echo hi}\n", encoding="utf-8")
    cfg = _cfg(d, allow_inline_code=True)
    cfg.inline_code_timeout_s = 17
    monkeypatch.setattr(P, "expand_prompt", fake_expand)

    spec = P.load_configured_prompt_spec(cfg)

    assert "expanded" in spec.system
    assert seen["allow_code"] is True
    assert seen["timeout_s"] == 17
