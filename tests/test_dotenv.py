from __future__ import annotations

from pathlib import Path

import pytest

from js import dotenv


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the parent walk at tmp_path and unplug the global config `.env`.

    `candidate_files` climbs to the filesystem root, so a real `.env` in /tmp or
    the operator's home would otherwise bleed into these assertions.
    """
    monkeypatch.setattr(dotenv._paths, "config_dir", lambda: tmp_path / "no-such-config")
    real = dotenv.candidate_files

    def bounded(cwd: Path | None = None) -> list[Path]:
        return [path for path in real(cwd) if tmp_path in path.parents]

    monkeypatch.setattr(dotenv, "candidate_files", bounded)


def test_parse_handles_comments_quotes_and_export() -> None:
    parsed = dotenv.parse(
        "\n".join(
            [
                "# a comment",
                "",
                "TAVILY_API_KEY=plain",
                "export EXA_API_KEY=exported",
                "SERPER_API_KEY='single'",
                'CONTEXT7_API_KEY="double"',
                "TRAILING=value # trailing comment",
                "SPACED = spaced ",
                "EMPTY=",
                "not an assignment",
                "=novalue",
            ]
        )
    )
    assert parsed == {
        "TAVILY_API_KEY": "plain",
        "EXA_API_KEY": "exported",
        "SERPER_API_KEY": "single",
        "CONTEXT7_API_KEY": "double",
        "TRAILING": "value",
        "SPACED": "spaced",
        "EMPTY": "",
    }


def test_load_fills_unset_names_and_never_overrides_real_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("TAVILY_API_KEY=from-file\nEXA_API_KEY=from-file\n")
    env = {"TAVILY_API_KEY": "from-shell"}
    applied = dotenv.load(cwd=tmp_path, environ=env)
    assert env == {"TAVILY_API_KEY": "from-shell", "EXA_API_KEY": "from-file"}
    assert tmp_path / ".env" in applied


def test_load_walks_up_and_nearest_file_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(tmp_path, monkeypatch)
    nested = tmp_path / "project" / "sub"
    nested.mkdir(parents=True)
    (tmp_path / ".env").write_text("TAVILY_API_KEY=outer\nSERPER_API_KEY=outer-only\n")
    (tmp_path / "project" / ".env").write_text("TAVILY_API_KEY=inner\n")
    env: dict[str, str] = {}
    dotenv.load(cwd=nested, environ=env)
    assert env == {"TAVILY_API_KEY": "inner", "SERPER_API_KEY": "outer-only"}


def test_load_is_a_no_op_without_any_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(tmp_path, monkeypatch)
    env: dict[str, str] = {}
    assert dotenv.load(cwd=tmp_path, environ=env) == []
    assert env == {}


def test_global_config_env_is_the_lowest_precedence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(tmp_path, monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text("TAVILY_API_KEY=global\nEXA_API_KEY=global-only\n")
    monkeypatch.setattr(dotenv._paths, "config_dir", lambda: config_dir)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("TAVILY_API_KEY=project\n")
    env: dict[str, str] = {}
    dotenv.load(cwd=project, environ=env)
    assert env == {"TAVILY_API_KEY": "project", "EXA_API_KEY": "global-only"}


def test_search_tool_reads_a_key_supplied_only_by_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end the feature exists for: tavily_search finds a .env-only key."""
    from js.toolkit import search

    _isolate(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("TAVILY_API_KEY=from-dotenv\n")
    # delenv records the pre-state, so the value load() writes into the real
    # os.environ is removed again at teardown.
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    dotenv.load(cwd=tmp_path)
    assert search._key("TAVILY_API_KEY") == "from-dotenv"
