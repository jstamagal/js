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
