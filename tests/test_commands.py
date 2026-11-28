"""The js commands: a first argument named in `js.commands.COMMANDS` runs that
command through the real entry point; any other first argument is the js
command line as before."""

from __future__ import annotations

from pathlib import Path

import pytest

from js import command_line, commands, entry, paths


@pytest.fixture
def project(tmp_path, monkeypatch):
    directory = tmp_path / "proj"
    directory.mkdir()
    monkeypatch.chdir(directory)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return directory


def _tree(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")} if root.exists() else set()


@pytest.mark.parametrize("name", sorted(commands.COMMANDS))
def test_each_command_has_its_own_help(name, capsys):
    with pytest.raises(SystemExit) as done:
        entry.main([name, "--help"])
    assert done.value.code == 0
    assert capsys.readouterr().out.startswith(f"Usage: js {name}")


@pytest.mark.parametrize("argv", [
    ["--init"], ["--init-full"], ["--init-agent", "dan"],
    ["--rotate"], ["--rotate", "30d"], ["--unrotate"], ["--unrotate", "s"], ["--dry-run"],
    ["-p", "x", "--rotate"],
])
def test_the_old_flags_are_refused_and_change_nothing(project, argv, capsys):
    here_before = _tree(project)
    with pytest.raises(SystemExit) as refused:
        entry.main(argv)
    assert refused.value.code == 2
    err = capsys.readouterr().err
    assert any(word in err for word in argv if word.startswith("--"))
    assert _tree(project) == here_before
    assert not paths.archive_root().exists()
    assert not (paths.global_agents_dir() / "dan").exists()


def test_a_word_that_names_no_command_is_refused_as_before(project):
    with pytest.raises(SystemExit) as refused:
        entry.main(["hello"])
    assert refused.value.code == 2
    assert _tree(project) == set()


@pytest.mark.parametrize("word", sorted(commands.COMMANDS))
def test_a_command_word_after_an_option_is_the_js_command_line(word):
    args = command_line.parser().parse_args(["--commit", word])
    assert args.commit and args.target == word


def test_init_full_with_an_agent_is_refused(project):
    with pytest.raises(SystemExit) as refused:
        entry.main(["init", "--full", "agent", "dan"])
    assert refused.value.code == 2
    assert _tree(project) == set()
    assert not (paths.global_agents_dir() / "dan").exists()


def test_init_agent_refuses_full(project):
    with pytest.raises(SystemExit) as refused:
        entry.main(["init", "agent", "dan", "--full"])
    assert refused.value.code == 2
    assert not (paths.global_agents_dir() / "dan").exists()


@pytest.mark.parametrize("before", [True, False])
def test_home_works_before_and_after_the_command(project, tmp_path, before):
    other = tmp_path / "other-home"
    argv = ["init", "agent", "dan"]
    argv = ["--home", str(other), *argv] if before else [*argv, "--home", str(other)]
    assert entry.main(argv) == 0
    assert (other / "agents/dan/agent.yaml").is_file()


def test_rotate_refuses_an_age_with_restore(project):
    with pytest.raises(SystemExit) as refused:
        entry.main(["rotate", "30d", "--restore"])
    assert refused.value.code == 2
    assert not paths.archive_root().exists()


@pytest.mark.parametrize("flag", ["--help", "--help-full"])
def test_the_help_lists_every_command_and_no_removed_flag(flag, capsys):
    with pytest.raises(SystemExit) as done:
        entry.main([flag])
    assert done.value.code == 0
    out = capsys.readouterr().out
    for name in commands.COMMANDS:
        assert f"js {name}" in out
    for gone in ("--init", "--rotate", "--unrotate", "--dry-run"):
        assert gone not in out
