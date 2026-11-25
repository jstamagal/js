"""The shared secret-patterns file, read by envctx (C) and js (Python)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from js import paths, secret_patterns

ROOT = Path(__file__).resolve().parent.parent
ENVCTX = ROOT / "tools" / "envctx.c"
STOCK = ROOT / "defaults" / "secret-patterns"

needs_cc = pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler for envctx")

SHA = "0123456789abcdef0123456789abcdef01234567"
HEX64 = SHA + "0123456789abcdef01234567"
BLOB = "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gH"


def run_envctx(home: Path, commands: list[str], **env: str) -> subprocess.CompletedProcess:
    hist = home / "hist"
    hist.write_text("".join(f"{c}\n" for c in commands))
    full = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "SHELL": "/bin/bash",
        "HISTFILE": str(hist),
        "ENVCTX_HIST": "12",
        **env,
    }
    return subprocess.run(["sh", str(ENVCTX)], env=full, capture_output=True, text=True,
                          timeout=60, cwd=home)


def history(out: str) -> list[str]:
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("recent_shell"))
    return [line.strip() for line in lines[start + 1:] if line.startswith("  ")]


# ---- Python reader -----------------------------------------------------------


@pytest.mark.parametrize("token", [
    "sk-foo-bar", "sk-xxxx", "sk-or-xxxx", "sk-ant-api03-abcdefgh", "nvapi-abcdefghijk",
    "gsk_abcdefghijk", "xai-abcdefghijk", "hf_abcdefghijk", "AIzaSyabcdefghijk",
    HEX64, BLOB,
])
def test_stock_file_calls_these_secrets(token):
    assert secret_patterns.load(STOCK).is_secret(token)


@pytest.mark.parametrize("token", ["free", "27b", "Q4_K_M", "auto", SHA, "sk-", "hf_abc"])
def test_stock_file_leaves_these_alone(token):
    assert not secret_patterns.load(STOCK).is_secret(token)


def test_longest_prefix_names_the_match():
    assert secret_patterns.load(STOCK).prefix("sk-ant-api03-abcdefgh") == "sk-ant-"


def test_home_file_replaces_the_stock_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "home", lambda: tmp_path)
    assert secret_patterns.patterns_file() == STOCK
    (tmp_path / "secret-patterns").write_text("class key a-z\nprefix zz9- 3 10 key\n")
    assert secret_patterns.patterns_file() == tmp_path / "secret-patterns"
    assert secret_patterns.is_secret("zz9-abcd")
    assert not secret_patterns.is_secret("sk-foo-bar")


@pytest.mark.parametrize("text, line", [
    ("bogus 1 2\n", 1),
    ("class key a-z\nprefix sk- x 9 key\n", 2),
    ("class key a-z\n\nprefix sk- 1 9 nope\n", 3),
    ("entropy 3 2\n", 1),
    ("class key z-a\n", 1),
])
def test_unreadable_line_names_file_and_line(text, line):
    with pytest.raises(secret_patterns.SecretPatternsError, match=rf"^f:{line}: "):
        secret_patterns.parse(text, "f")


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(secret_patterns.SecretPatternsError, match="nope"):
        secret_patterns.load(tmp_path / "nope")


@pytest.mark.parametrize("content", [b"\xff\xfe\x00bad", b"class key a-z\n\xc3("])
def test_non_utf8_file_names_the_file(tmp_path, content):
    bad = tmp_path / "secret-patterns"
    bad.write_bytes(content)

    with pytest.raises(secret_patterns.SecretPatternsError) as error:
        secret_patterns.load(bad)

    assert str(bad) in str(error.value)


# ---- envctx reader -----------------------------------------------------------


@needs_cc
def test_envctx_scrubs_with_the_stock_file(tmp_path):
    out = run_envctx(tmp_path, [
        "echo sk-ant-api03-abcdefgh",
        f"echo {SHA}",
        f"echo {HEX64}",
        f"echo {BLOB}",
        "echo nvapi-abcdefghijk",
        "ls Q4_K_M",
    ]).stdout
    lines = history(out)
    assert "sk-ant-api03-abcdefgh" not in out and "<key:sk-ant-..>" in lines[0]
    assert SHA in lines[1]
    assert HEX64 not in out
    assert BLOB not in out
    assert "nvapi-abcdefghijk" not in out
    assert "Q4_K_M" in lines[5]


@needs_cc
def test_one_new_line_teaches_both_readers(tmp_path):
    token = "zz9-abcdefgh"
    assert not secret_patterns.load(STOCK).is_secret(token)
    assert token in run_envctx(tmp_path, [f"echo {token}"]).stdout

    mine = tmp_path / "mine"
    mine.write_text(STOCK.read_text() + "prefix zz9- 4 64 key\n")
    assert secret_patterns.load(mine).is_secret(token)
    out = run_envctx(tmp_path, [f"echo {token}"], JS_SECRET_PATTERNS=str(mine)).stdout
    assert token not in out


@needs_cc
def test_envctx_reads_the_home_file_when_present(tmp_path):
    (tmp_path / ".js").mkdir()
    (tmp_path / ".js" / "secret-patterns").write_text("class key a-z\nprefix zz9- 4 64 key\n")
    out = run_envctx(tmp_path, ["echo zz9-abcdefgh", "echo sk-ant-api03-abcdefgh"]).stdout
    assert "zz9-abcdefgh" not in out
    assert "sk-ant-api03-abcdefgh" in out


@needs_cc
def test_envctx_fails_on_an_unreadable_file(tmp_path):
    bad = tmp_path / "bad"
    bad.write_text("class key a-z\nprefix sk- x 9 key\n")
    proc = run_envctx(tmp_path, ["echo hi"], JS_SECRET_PATTERNS=str(bad))
    assert proc.returncode != 0
    assert f"{bad}:2" in proc.stderr
    assert proc.stdout == ""

    proc = run_envctx(tmp_path, ["echo hi"], JS_SECRET_PATTERNS=str(tmp_path / "gone"))
    assert proc.returncode != 0
    assert str(tmp_path / "gone") in proc.stderr


# ---- envctx history skip and rules ------------------------------------------


@needs_cc
def test_skip_steps_past_leading_assignments(tmp_path):
    lines = history(run_envctx(tmp_path, [
        "echo kept",
        "JS_PROVIDER=x js -p foo",
        "FOO=1 BAR=two /usr/bin/js -d -p foo",
        "js -p plain",
        "FOO=1 echo also-kept",
    ], ENVCTX_SKIP="js").stdout)
    assert lines == ["echo kept", "FOO=1 echo also-kept"]


@needs_cc
@pytest.mark.parametrize("mode", ["headless", "repl", None])
def test_envctx_sends_no_turn_ending_rule(tmp_path, mode):
    env = {"JS_MODE": mode} if mode else {}
    out = run_envctx(tmp_path, ["echo hi"], **env).stdout
    assert not [line for line in out.splitlines() if line.startswith("rule:")]


def test_prompt_expansion_names_the_file_for_envctx(tmp_path, monkeypatch):
    from js import persona

    monkeypatch.setattr(paths, "home", lambda: tmp_path)
    monkeypatch.delenv("JS_SECRET_PATTERNS", raising=False)
    cfg = type("Cfg", (), {"settings": {"SHELL_PROGRAM": "sh"}, "allow_inline_code": False})()

    assert persona.expand_agent_text("{{JS_SECRET_PATTERNS}}", cfg) == str(STOCK)
