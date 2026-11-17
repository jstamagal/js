"""Profile selection precedes setup reads and follows every jail boundary."""
import json
import os
import subprocess
import sys

import pytest
from jail_probe import needs_bwrap

from js import jail, paths
from js.toolkit import ToolContext, shell_fetch


@pytest.mark.parametrize('source', ['environment', 'default'])
def test_fresh_cli_selects_home_before_reading_tool_setup(tmp_path, source):
    user = tmp_path / 'user'
    losing = tmp_path / 'losing' if source == 'environment' else user / '.js'
    malformed = losing / 'tool_descriptions/read.md'
    malformed.parent.mkdir(parents=True)
    malformed.write_text('Custom contract without parameter definitions.\n')
    selected = tmp_path / 'selected'
    env = {**os.environ, 'HOME': str(user)}
    env.pop('JS_HOME', None)
    if source == 'environment':
        env['JS_HOME'] = str(losing)
    result = subprocess.run([sys.executable, '-m', 'js', '--home', str(selected),
                             '--list-agents', '--json'], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert selected.is_dir()
    assert 'defaultagent' in json.loads(result.stdout)


def test_malformed_selected_tool_setup_exits_two(tmp_path):
    selected = tmp_path / 'selected'
    malformed = selected / 'tool_descriptions/read.md'
    malformed.parent.mkdir(parents=True)
    malformed.write_text('Contract without parameter definitions.\n')
    result = subprocess.run([sys.executable, '-m', 'js', '--home', str(selected),
                             '-p', 'hello', '--system-prompt', 'System', '--no-save'],
                            capture_output=True, text=True)
    assert result.returncode == 2, result.stderr
    assert str(malformed) in result.stderr


def _jail(tmp_path, monkeypatch, *, profile_in_user=False):
    user = tmp_path / 'user'
    user.mkdir()
    monkeypatch.setenv('HOME', str(user))
    selected = paths.configure_home(user / 'profile' if profile_in_user else tmp_path / 'profile')
    root = user if profile_in_user else tmp_path / 'project'
    root.mkdir(exist_ok=True)
    private = tmp_path / 'private'
    (private / 'tmp').mkdir(parents=True)
    (private / 'js-tmp').mkdir()
    return jail.Jail(root=root, bwrap='bwrap', private=private), user, selected


def test_jail_mounts_selected_tmp_without_creating_default_home(tmp_path, monkeypatch):
    current, user, selected = _jail(tmp_path, monkeypatch)
    argv = current.argv(['true'])
    mounts = [(argv[i + 1], argv[i + 2]) for i, arg in enumerate(argv[:-2]) if arg == '--bind']
    assert (str(current.js_tmp), str(selected / 'tmp')) in mounts
    assert not (user / '.js').exists()
    target = selected / 'tmp' / 'roundtrip'
    current.js_tmp.joinpath('roundtrip').write_text('private')
    assert current.confine(target).read_text() == 'private'
    assert current.shown(str(current.js_tmp / 'roundtrip')) == str(target)


def test_default_hide_protects_selected_profile_beneath_project(tmp_path, monkeypatch):
    current, user, selected = _jail(tmp_path, monkeypatch, profile_in_user=True)
    key = selected / 'logins/sentinel.json'
    key.parent.mkdir()
    key.write_text('sentinel')
    assert selected in current.hides()
    with pytest.raises(jail.JailError):
        current.confine(key)
    assert current.confine(key, hide=[]).read_text() == 'sentinel'
    argv = current.argv(['true'])
    assert any(str(selected) in arg for arg in argv)


@needs_bwrap
@pytest.mark.parametrize('profile_in_user', [False, True])
def test_shell_and_file_tools_share_selected_private_tmp(tmp_path, monkeypatch, profile_in_user):
    current, user, selected = _jail(tmp_path, monkeypatch, profile_in_user=profile_in_user)
    project = current.root
    key = selected / 'logins/sentinel'
    key.parent.mkdir()
    key.write_text('sentinel')
    monkeypatch.chdir(project)
    jail.enter(project, setting=())
    context = ToolContext(cwd=project)
    context.jail_bind = ()
    result = shell_fetch.shell('printf roundtrip > "$JS_HOME/tmp/check"; '
                               'cat "$JS_HOME/tmp/check"; test ! -e "$JS_HOME/logins/sentinel"',
                               context=context)
    assert 'exit=0' in result, result
    assert jail.active().confine(selected / 'tmp/check').read_text() == 'roundtrip'
    assert not (user / '.js').exists()
