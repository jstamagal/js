"""/set in the REPL: what it prints, what NAME selects, and /help set NAME."""

from __future__ import annotations

from repl_driver import output_of, record_turns, type_into_js

from js import compaction, logins, messages as msgs, settings


def _out(monkeypatch, tmp_path, capsys, line: str, *argv: str) -> list[str]:
    """The lines `line` printed, typed into js started in `tmp_path` with `argv`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(msgs, "BANNER", msgs.BANNER)
    turns = record_turns(monkeypatch)
    printed: list[str] = []

    async def typing(enter):
        printed.append(await output_of(enter, capsys, line))

    assert type_into_js(monkeypatch, typing, *argv) == 0
    assert turns == []
    return printed[0].splitlines()


def test_set_group_lists_that_group(monkeypatch, tmp_path, capsys):
    lines = _out(monkeypatch, tmp_path, capsys, "/set compaction")
    group = [spec.key for spec in settings.REGISTRY if spec.section == "compaction"]
    assert [line.split()[1] for line in lines] == group


def test_set_wildcard_lists_names_that_match(monkeypatch, tmp_path, capsys):
    lines = _out(monkeypatch, tmp_path, capsys, "/set *_FILE")
    files = [spec.key for spec in settings.REGISTRY if spec.key.endswith("_FILE")]
    assert files and [line.split()[1] for line in lines] == files


def test_set_context_size_is_the_window_of_the_next_request(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    turns = record_turns(monkeypatch)

    assert type_into_js(monkeypatch, ["/set CONTEXT_SIZE 123456", "hello"]) == 0

    [turn] = turns
    assert compaction.configured_context_window(turn.cfg, lambda: None) == 123456


def test_set_model_shows_the_model_the_agent_runs(monkeypatch, tmp_path, capsys):
    agent = tmp_path / ".js" / "agents" / "reviewer"
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text("model: deepseek/review-agent-pick\n", encoding="utf-8")
    (agent / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    logins.save_login(logins.Login("deepseek", provider_api_key="offline"))

    [line] = _out(monkeypatch, tmp_path, capsys, "/set model", "--agent", "reviewer")

    assert "review-agent-pick" in line and "agent reviewer" in line


def test_banner_changes_can_belong_to_the_caller():
    from js import setcmd

    before = msgs.BANNER
    changed = []
    values = {}
    result = setcmd.set_command(values, "BANNER custom", banner_changed=changed.append)
    assert result.changed and changed == ["custom"]
    setcmd.set_command(values, "-BANNER", {"BANNER": before}, banner_changed=changed.append)
    assert changed == ["custom", before]
    assert msgs.BANNER == before
