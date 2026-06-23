"""`js --migrate-config`: one-shot legacy config.toml -> jsrc set-script."""

from __future__ import annotations

from js import cli


def test_migrate_writes_jsrc_set_lines_from_toml(tmp_path, monkeypatch):
    legacy = tmp_path / "config.toml"
    target = tmp_path / "jsrc"
    legacy.write_text(
        '[model]\nid = "deepseek/deepseek-v4-flash"\n[limits]\nmax_tool_iterations = 50\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: legacy)
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: target)

    assert cli._run_migrate_config() == 0
    text = target.read_text(encoding="utf-8")
    assert "set model.id deepseek/deepseek-v4-flash" in text
    assert "set limits.max_tool_iterations 50" in text


def test_migrate_no_legacy_returns_1(tmp_path, monkeypatch):
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: tmp_path / "missing.toml")
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: tmp_path / "jsrc")
    assert cli._run_migrate_config() == 1


def test_migrate_refuses_when_target_exists(tmp_path, monkeypatch):
    legacy = tmp_path / "config.toml"
    legacy.write_text('[model]\nid = "x"\n', encoding="utf-8")
    target = tmp_path / "jsrc"
    target.write_text("set model.id x\n", encoding="utf-8")
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: legacy)
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: target)
    assert cli._run_migrate_config() == 1
