"""--printonly dry-run family + the --im-a-pussy inline-code opt-out.

printonly NEVER errors — every path degrades to a warning and still prints.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from js import cli
from js.cli import _printonly_slots, _printonly_letters


def _write_agent(root: Path) -> Path:
    """A minimal offline agent: a tools manifest + persona text with an env
    directive and a code directive so p/e/i visibly differ."""
    d = root / ".js" / "agents" / "potest"
    d.mkdir(parents=True)
    (d / "00-tools.yaml").write_text("tools:\n  - read\n  - shell\n", encoding="utf-8")
    (d / "01-prompt.md").write_text(
        "Role line.\nenv=<{{WHO}}>\ncode=<!{sh printf ran}>\n", encoding="utf-8"
    )
    (d / "01-benchmark.md").write_text("Bench turn for {{WHO}}.", encoding="utf-8")
    return d


@pytest.fixture()
def offline_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.delenv("JS_ALLOW_INLINE_CODE", raising=False)
    monkeypatch.setenv("WHO", "ape")
    monkeypatch.chdir(tmp_path)
    _write_agent(tmp_path)
    return tmp_path


# ---- slot parser ---------------------------------------------------------

def test_slots_letters_only():
    assert _printonly_slots("tp") == ("tp", None, None)


def test_slots_letters_count():
    assert _printonly_slots("p:20") == ("p", 20, None)


def test_slots_letters_count_path():
    assert _printonly_slots("p:20:/tmp/x.md") == ("p", 20, "/tmp/x.md")


def test_slots_empty_count_slot_skips():
    # p::/path — empty count slot skips, path still lands.
    assert _printonly_slots("p::/tmp/agent-foo.md") == ("p", None, "/tmp/agent-foo.md")


def test_slots_path_with_colon_survives():
    # split(":", 2) keeps a colon inside the path intact.
    assert _printonly_slots("p::/tmp/a:b.md") == ("p", None, "/tmp/a:b.md")


def test_slots_bad_count_degrades_to_none(capsys):
    assert _printonly_slots("p:notanint:/tmp/x")[1] is None
    assert "non-numeric count" in capsys.readouterr().err


# ---- letter resolution ---------------------------------------------------

def test_letters_all_expands_to_every_section():
    assert _printonly_letters("a") == list("tpeib")


def test_letters_dedup_and_order_preserved():
    assert _printonly_letters("pip") == ["p", "i"]


def test_letters_unknown_warns_and_skips(capsys):
    assert _printonly_letters("pXt") == ["p", "t"]
    assert "unknown letter" in capsys.readouterr().err


def test_letters_o_is_parked(capsys):
    assert _printonly_letters("po") == ["p"]
    assert "parked" in capsys.readouterr().err


def test_letters_nothing_valid_falls_back_to_all(capsys):
    assert _printonly_letters("XYZ") == list("tpeib")


# ---- end-to-end through main() -------------------------------------------

def test_printonly_prompt_raw(offline_agent, capsys):
    rc = cli.main(["-a", "potest", "--printonly=p"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "{{WHO}}" in out              # raw: env not expanded
    assert "!{sh printf ran}" in out     # raw: code not run


def test_printonly_prompt_env_expanded(offline_agent, capsys):
    rc = cli.main(["-a", "potest", "--printonly=e"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "env=<ape>" in out            # {{WHO}} resolved
    assert "!{sh printf ran}" in out     # code left literal in env-only pass


def test_printonly_prompt_inlines_expanded(offline_agent, capsys):
    rc = cli.main(["-a", "potest", "--printonly=i"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "env=<ape>" in out
    assert "code=<ran>" in out           # code actually ran (on by default)


def test_printonly_inlines_respect_opt_out(offline_agent, capsys):
    rc = cli.main(["-a", "potest", "--im-a-pussy", "--printonly=i"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "code=<!{sh printf ran}>" in out  # opted out: left literal, not run


def test_printonly_tools_json(offline_agent, capsys):
    rc = cli.main(["-a", "potest", "--printonly=t"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"read"' in out and '"shell"' in out


def test_printonly_benchmarks(offline_agent, capsys):
    rc = cli.main(["-a", "potest", "--printonly=b"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Bench turn for ape." in out  # bench expanded too


def test_printonly_default_is_everything(offline_agent, capsys):
    rc = cli.main(["-a", "potest", "--printonly"])
    out = capsys.readouterr().out
    assert rc == 0
    for title in ("TOOLS", "PROMPT (raw)", "PROMPT (env-expanded)",
                  "PROMPT (inlines-expanded)", "BENCHMARKS"):
        assert title in out


def test_printonly_count_slices_output(offline_agent, capsys):
    rc = cli.main(["-a", "potest", "--printonly=p:2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(out.rstrip("\n").splitlines()) == 2


def test_printonly_path_writes_file(offline_agent, capsys, tmp_path):
    dest = tmp_path / "dump.md"
    rc = cli.main(["-a", "potest", f"--printonly=p::{dest}"])
    assert rc == 0
    assert capsys.readouterr().out == ""       # nothing to stdout
    assert "{{WHO}}" in dest.read_text(encoding="utf-8")


def test_printonly_unwritable_path_falls_back_to_stdout(offline_agent, capsys):
    rc = cli.main(["-a", "potest", "--printonly=p::/no/such/dir/x.md"])
    err = capsys.readouterr()
    assert rc == 0
    assert "could not write" in err.err
    assert "{{WHO}}" in err.out                 # printed to stdout instead


def test_printonly_unknown_letter_still_prints(offline_agent, capsys):
    rc = cli.main(["-a", "potest", "--printonly=pZ"])
    err = capsys.readouterr()
    assert rc == 0
    assert "unknown letter" in err.err
    assert "{{WHO}}" in err.out                 # the p section still printed


def test_printonly_missing_agent_never_errors(offline_agent, capsys):
    # A nonexistent agent must not traceback — printonly degrades and exits 0.
    rc = cli.main(["-a", "nosuchagent", "--printonly=p"])
    assert rc == 0
