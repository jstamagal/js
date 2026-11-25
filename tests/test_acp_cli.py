import os
import sys
from types import SimpleNamespace

import pytest

from js import cli


def test_acp_forwards_run_options_with_absolute_home(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", object())
    monkeypatch.setattr(cli._home, "migrate_once", lambda: print("migration diagnostic"))

    def run(options):
        assert options == ["--agent", "example", "-m", "provider/model", "-r", "high",
                           "--no-tools", "--extra", "ALLOW_INLINE_CODE=off"]
        assert os.environ["JS_HOME"] == str(tmp_path / "profile")
        print('{"jsonrpc":"2.0"}')
        return 7

    monkeypatch.setattr("js.acp_agent", SimpleNamespace(run=run), raising=False)
    assert cli.main(["--home", "profile", "--agent", "example", "--acp",
                     "-m", "provider/model", "-r", "high", "--no-tools",
                     "--extra", "ALLOW_INLINE_CODE=off", "--"]) == 7
    streams = capsys.readouterr()
    assert streams.out == '{"jsonrpc":"2.0"}\n'
    assert "migration diagnostic" in streams.err


@pytest.mark.parametrize("options", [
    ["-C", "project"], ["-s"], ["-qn"], ["--last"], ["-p"], ["--json"],
    ["--json-stream"], ["--list-models"], ["--login"], ["--bench", "example"],
    ["--help"], ["--", "target"],
])
def test_acp_rejects_cli_modes(options, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--acp", *options])
    assert exc.value.code == 2
    streams = capsys.readouterr()
    assert streams.out == ""
    assert streams.err


@pytest.mark.parametrize(("options", "prompt", "target"), [
    (["--prompt=--acp"], "--acp", None),
    (["-p", "explain --acp"], "explain --acp", None),
    (["--commit", "--", "--acp"], None, "--acp"),
])
def test_acp_in_prompt_or_positional_uses_normal_dispatch(monkeypatch, options, prompt, target):
    def run(args, argv):
        assert not args.acp
        assert args.prompt == prompt
        assert args.target == target
        return 0

    monkeypatch.setattr(cli, "_run_args", run)
    assert cli.main(options) == 0
