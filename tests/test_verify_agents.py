"""Verification follows the same effective agent files as a run."""
from __future__ import annotations

import pytest

from js import paths
from js.verify import verify


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def layers(tmp_path, monkeypatch):
    stock, profile, project = (tmp_path / name for name in ('stock', 'profile', 'project'))
    (project / '.git').mkdir(parents=True)
    paths.configure_home(profile)
    monkeypatch.setattr(paths, 'defaults_dir', lambda: stock)
    monkeypatch.chdir(project)
    return stock, profile, project / '.js'


@pytest.mark.parametrize('agent', [None, 'active'])
def test_verify_ignores_shadowed_invalid_stock_manifest(layers, agent):
    stock, profile, _ = layers
    _write(stock / 'agents/active/agent.yaml', 'tools: invalid\n')
    _write(stock / 'agents/active/01-prompt.md', 'stock prompt')
    _write(profile / 'agents/active/agent.yaml', 'tools: [read:eager]\n')
    assert verify(agent) == []


@pytest.mark.parametrize('agent', [None, 'active'])
def test_verify_accepts_empty_higher_agent_directory(layers, agent):
    stock, profile, _ = layers
    _write(stock / 'agents/active/agent.yaml', 'tools: [read:eager]\n')
    _write(stock / 'agents/active/01-prompt.md', 'stock prompt')
    (profile / 'agents/active').mkdir(parents=True)
    assert verify(agent) == []


@pytest.mark.parametrize('agent', [None, 'empty'])
def test_verify_accepts_agent_existing_only_as_empty_directory(layers, agent):
    _, profile, _ = layers
    (profile / 'agents/empty').mkdir(parents=True)
    assert verify(agent) == []


@pytest.mark.parametrize('agent', [None, 'active'])
def test_verify_reports_invalid_effective_home_manifest(layers, agent):
    stock, profile, _ = layers
    _write(stock / 'agents/active/agent.yaml', 'tools: [read:eager]\n')
    effective = _write(profile / 'agents/active/agent.yaml', 'tools: invalid\n')
    errors = verify(agent)
    assert len(errors) == 1
    assert str(effective) in errors[0]


@pytest.mark.parametrize('manifest', [
    'tools: [tag:missing]\n',
    'tools: [read:eager]\nmodels:\n  - match: "*"\n    tools: [tag:missing]\n',
])
def test_verify_validates_effective_agent_tool_policies(layers, manifest):
    _, profile, _ = layers
    effective = _write(profile / 'agents/active/agent.yaml', manifest)
    errors = verify()
    assert len(errors) == 1
    assert str(effective.parent) in errors[0]
    assert 'missing' in errors[0]


def test_verify_keeps_active_preset_scope_without_descending_worktrees(layers):
    _, profile, project = layers
    nested = _write(profile / 'worktrees/other/jsrc', 'set TURN_ROUNDS_MAX invalid\n')
    own = _write(profile / 'jsrc.broken', 'set TURN_ROUNDS_MAX invalid\n')
    local = _write(project / 'jsrc.broken', 'set TURN_ROUNDS_MAX invalid\n')
    errors = verify()
    assert any(str(own) in error for error in errors)
    assert any(str(local) in error for error in errors)
    assert not any(str(nested) in error for error in errors)
