"""A selected home contains the complete profile for one invocation."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import ai
import pytest

from js import cli, home, paths, persona, promptexpand, runtime, session_catalog, skills
from js.model_client import ModelStreamResult
from js.toolkit import ToolContext, process_net


def _tree(root):
    return {str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in (root, *root.rglob('*'))}


def test_home_resolution_is_absolute_once_and_flag_beats_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('JS_HOME', str(tmp_path / 'environment'))
    chosen = paths.configure_home('selected')
    assert chosen == tmp_path / 'selected'
    assert chosen.is_dir()
    assert os.environ['JS_HOME'] == str(chosen)
    monkeypatch.setenv('JS_HOME', str(tmp_path / 'changed'))
    monkeypatch.chdir(chosen)
    assert paths.home() == chosen
    assert paths.configure_home() == tmp_path / 'changed'


def test_missing_home_flag_and_non_directory_are_exit_two(tmp_path, capsys):
    file = tmp_path / 'file'
    file.write_text('keep')
    assert cli.main(['--home', str(file), '--help']) == 2
    assert file.read_text() == 'keep'
    assert str(file) in capsys.readouterr().err
    assert cli.main(['--home']) == 2


@pytest.mark.parametrize('selection', ['flag', 'environment', 'flag_over_env'])
def test_full_prompt_turn_does_not_touch_default_profile(tmp_path, monkeypatch, selection):
    user = tmp_path / 'user'
    sentinel = user / '.js'
    sentinel.mkdir(parents=True)
    (sentinel / 'sentinel').write_text('unchanged')
    (sentinel / 'jsrc').write_text('set MODEL must-not-read-this-model\n')
    shared = user / '.agents/skills/private'
    shared.mkdir(parents=True)
    (shared / 'SKILL.md').write_text('---\nname: private\ndescription: real profile only\n---\nSECRET')
    legacy = user / '.config/js'
    legacy.mkdir(parents=True)
    (legacy / 'jsrc').write_text('set MODEL must-not-migrate\n')
    project = tmp_path / 'project'
    project.mkdir()
    selected = tmp_path / 'selected profile'
    monkeypatch.setenv('HOME', str(user))
    monkeypatch.chdir(project)
    argv = ['-p', 'hello', '-q']
    if selection != 'flag':
        monkeypatch.setenv('JS_HOME', str(selected if selection == 'environment' else tmp_path / 'losing profile'))
    if selection != 'environment':
        argv[:0] = ['--home', str(selected)]
    before, legacy_before = _tree(sentinel), _tree(legacy)
    requests = []

    def stream(**kwargs):
        requests.append(kwargs)
        return ModelStreamResult(text='complete', tool_calls=[], reasoning='', usage=None,
            finish_reason='stop', assistant_message=ai.assistant_message('complete'))

    monkeypatch.setattr(runtime.model_client, 'stream_model_async', stream)
    assert cli.main(argv) == 0
    assert len(requests) == 1
    assert paths.home() == selected
    assert _tree(sentinel) == before
    assert _tree(legacy) == legacy_before
    assert not (tmp_path / 'losing profile').exists()
    assert 'private' not in {skill.name for skill in skills.discover_skills(project).skills}
    assert (selected / 'sessions').is_dir()
    sessions = list((selected / 'sessions').rglob('*.jsonl'))
    assert len(sessions) == 1
    starts = [json.loads(line) for line in sessions[0].read_text().splitlines()
              if json.loads(line).get('kind') == 'session_metadata']
    assert starts[-1]['home'] == str(selected)


def test_selected_home_replaces_profile_even_when_started_in_user_home(tmp_path, monkeypatch):
    monkeypatch.chdir(Path.home())
    real_config = paths.home() / 'jsrc'
    real_config.parent.mkdir(parents=True)
    real_config.write_text('set MODEL real-profile\n')
    selected = paths.configure_home(tmp_path / 'selected')
    from js.config import from_env
    cfg = from_env(save_session=False)
    assert cfg.model != 'real-profile'
    assert cfg.prompt_roots[1:] == (selected / 'agents', selected / 'agents')
    assert paths.find('jsrc') == paths.defaults_dir() / 'jsrc'


def test_home_is_exported_in_all_prompt_forms_and_shell_tool(tmp_path, monkeypatch):
    selected = paths.configure_home(tmp_path / 'selected profile')
    cfg = SimpleNamespace(session_file=tmp_path / 's.jsonl', allow_inline_code=True)
    text = '{{JS_HOME}}|%%JS_HOME%%|!{sh printf %s "$JS_HOME"}'
    assert persona.expand_agent_text(text, cfg) == '|'.join([str(selected)] * 3)
    assert promptexpand.session_variables(cfg)['JS_HOME'] == str(selected)
    context = ToolContext(cwd=tmp_path, shell_env_allow=[])
    result = process_net.shell('printf %s "$JS_HOME"', context=context)
    assert str(selected) in str(result)


def test_custom_home_does_not_run_legacy_migration(tmp_path, monkeypatch):
    legacy = paths.legacy_homes()['config']
    legacy.mkdir(parents=True)
    old = legacy / 'jsrc'
    old.write_text('set MODEL old\n')
    paths.configure_home(tmp_path / 'selected')
    assert home.migrate_once() == []
    assert old.read_text() == 'set MODEL old\n'
    assert not paths.home_migration_marker().exists()


def test_session_metadata_records_current_home(tmp_path):
    selected = paths.configure_home(tmp_path / 'selected')
    path = tmp_path / 's.jsonl'
    session_catalog.record_session_start(path, cwd=tmp_path, agent='test')
    record = json.loads(path.read_text().splitlines()[0])
    assert record['home'] == str(selected)


def test_home_preprocessor_preserves_dispatch_and_literal_arguments(tmp_path):
    from js.home_cli import configure_home
    chosen = tmp_path / 'chosen'
    assert configure_home([f'--home={chosen}', 'login', 'provider']) == ['login', 'provider']
    assert paths.home() == chosen
    assert configure_home(['-p', 'text --home is literal', '--', '--home', 'literal']) == [
        '-p', 'text --home is literal', '--', '--home', 'literal']
    assert paths.home() == chosen


def test_continue_hint_carries_the_selected_home(tmp_path, monkeypatch, capsys):
    selected = paths.configure_home(tmp_path / 'selected profile')
    monkeypatch.setattr(runtime.model_client, 'stream_model_async', lambda **kwargs:
        ModelStreamResult(text='complete', tool_calls=[], reasoning='', usage=None,
            finish_reason='stop', assistant_message=ai.assistant_message('complete')))
    monkeypatch.chdir(tmp_path)
    assert cli.main(['-p', 'hello']) == 0
    import shlex
    line = next(line for line in capsys.readouterr().err.splitlines() if 'Continue' in line)
    command = shlex.split(line.split('Continue', 1)[1].lstrip(': '))
    assert command[command.index('--home') + 1] == str(selected)


def test_stock_personal_addins_follow_the_profile(tmp_path):
    selected = paths.configure_home(tmp_path / 'selected profile')
    own = selected / 'custom/role.md'
    own.parent.mkdir(parents=True)
    own.write_text('SELECTED_PROFILE_ROLE')
    real = paths.user_home() / '.js/custom/role.md'
    real.parent.mkdir(parents=True)
    real.write_text('REAL_PROFILE_ROLE')
    cfg = SimpleNamespace(session_file=tmp_path / 's.jsonl', allow_inline_code=False)
    source = (paths.defaults_dir() / 'agents/defaultagent/01-prompt.md').read_text()
    expanded = persona.expand_agent_text(source, cfg)
    assert 'SELECTED_PROFILE_ROLE' in expanded
    assert 'REAL_PROFILE_ROLE' not in expanded


def test_jail_host_tmp_mapping_uses_selected_profile(tmp_path):
    from js import jail
    selected = paths.configure_home(tmp_path / 'selected')
    private = tmp_path / 'private'
    instance = jail.Jail(root=tmp_path / 'project', bwrap='bwrap', private=private)
    assert instance.host_path(selected / 'tmp/probe') == private / 'js-tmp/probe'
    assert instance.shown(str(private / 'js-tmp/probe')) == str(selected / 'tmp/probe')
