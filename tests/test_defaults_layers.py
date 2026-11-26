"""Stock setup remains available when a profile overrides individual files."""
import pytest

from js import config, fset, paths, persona
from js.toolkit import descriptions, policy


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_find_uses_files_and_preserves_empty_override(tmp_path, monkeypatch):
    stock, own = (tmp_path / name for name in ('stock', 'own'))
    user = tmp_path / 'user'
    user.mkdir()
    monkeypatch.setenv('HOME', str(user))
    monkeypatch.setattr(paths, 'defaults_dir', lambda: stock)
    monkeypatch.setattr(paths, 'home', lambda: own)
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.git').mkdir()
    _write(stock / 'agents/a/01-prompt.md', 'stock')
    _write(own / 'agents/a/01-prompt.md', '')
    _write(tmp_path / '.js/agents/a/02-env.md', 'project')
    _write(stock / 'themes/stock', '00 black')
    _write(own / 'themes/own', '')
    _write(tmp_path / '.js/themes/stock', 'project must not select themes')
    assert paths.find('agents/a/01-prompt.md') == own / 'agents/a/01-prompt.md'
    assert paths.find('agents/a/01-prompt.md').read_text() == ''
    assert paths.find('agents/a/02-env.md') == tmp_path / '.js/agents/a/02-env.md'
    assert paths.names('themes') == ['own', 'stock']
    assert paths.find('themes/stock') == stock / 'themes/stock'
    assert paths.find('absent') is None


def test_find_answers_when_its_cache_is_cleared_under_it(tmp_path, monkeypatch):
    # Another thread clearing the full cache right after this one stores its
    # answer must not turn the lookup into a KeyError.
    class ClearedAfterStore(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            self.clear()

    stock, own = tmp_path / 'stock', tmp_path / 'own'
    monkeypatch.setattr(paths, 'defaults_dir', lambda: stock)
    monkeypatch.setattr(paths, 'home', lambda: own)
    monkeypatch.setattr(paths, '_FOUND', ClearedAfterStore())
    monkeypatch.chdir(tmp_path)
    _write(stock / 'themes/stock', '00 black')

    assert paths.find('themes/stock') == stock / 'themes/stock'


def test_a_home_default_agent_replaces_the_stock_one_whole(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    own = paths.home()
    override = _write(own / 'agents/defaultagent/01-prompt.md', 'replacement prompt')
    roots = (paths.defaults_dir() / 'agents', own / 'agents', tmp_path / '.js/agents')
    spec = persona.load_agent_prompt_spec('defaultagent', roots)
    assert spec.system.strip() == override.read_text()
    assert spec.tool_selectors == ()


def test_home_and_stock_themes_list_and_empty_theme_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stock_names = {p.name for p in (paths.defaults_dir() / 'themes').iterdir()}
    theme = _write(paths.home() / 'themes/personal', '00 #123456\n')
    assert set(fset.themes()) == stock_names | {'personal'}
    assert fset.theme_path('personal') == theme
    _write(paths.home() / 'themes/bitchx', '')
    assert fset.theme_entries('bitchx') == {}


def test_every_stock_file_uses_default_when_no_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for path in paths.defaults_dir().rglob('*'):
        if path.is_file():
            assert paths.find(path.relative_to(paths.defaults_dir())) == path


def test_descriptions_and_tools_yaml_are_file_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(paths.home() / 'tool_descriptions/shell.md', '')
    descriptions.reset_cache()
    assert descriptions.load_description('shell') == ''
    stock = tmp_path / 'stock'
    monkeypatch.setattr(paths, 'defaults_dir', lambda: stock)
    _write(stock / 'tools.yaml', 'tags:\n  review: [read:eager]\n')
    assert policy.load_tools_config().tags['review'] == ('read:eager',)
    _write(paths.home() / 'tools.yaml', '')
    assert policy.load_tools_config().tags == {}


def test_real_config_reports_the_agent_directory_that_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    own = paths.home() / 'agents/defaultagent'
    _write(own / '01-prompt.md', 'custom')
    cfg = config.from_env(cwd=tmp_path, save_session=False)
    assert cfg.prompts_dir == (own,)
    assert config.Config(model='test', prompts_dir=own).prompts_dir == own


def test_unknown_agent_names_searched_directories(tmp_path):
    roots = tuple(tmp_path / name for name in ('stock', 'home', 'project'))
    with pytest.raises(FileNotFoundError) as error:
        persona.load_agent_prompt_spec('missing', roots)
    assert 'missing' in str(error.value)
    assert all(str(root) in str(error.value) for root in roots)


def test_help_descriptions_and_fset_follow_home_when_profiles_change(tmp_path, monkeypatch):
    from js import settings
    for name in ('first', 'second'):
        own = tmp_path / name
        monkeypatch.setattr(paths, 'home', lambda own=own: own)
        _write(own / 'help/set.txt', f'TEST\n  {name}\n')
        _write(own / 'tool_descriptions/shell.md', name)
        _write(own / 'fset', f'fset TEST {name}\n')
        _write(own / 'themes/custom', f'fset 00 {name}\n')
        assert settings.setting_help()['TEST'] == name
        assert descriptions.load_description('shell') == name
        assert fset.defaults()['TEST'] == name
        assert fset.theme_entries('custom')['00'] == name


def test_unknown_theme_names_searched_directories():
    error = str(fset.theme_error('missing'))
    assert 'missing' in error
    assert all(str(root / 'themes') in error for root in paths.layers()[:2])
    with pytest.raises(ValueError):
        fset.theme_entries('missing')


def test_unknown_theme_in_jsrc_exits_two_before_model(tmp_path, monkeypatch, capsys):
    from js import cli, runtime
    monkeypatch.chdir(tmp_path)
    _write(paths.home() / 'jsrc', 'fset THEME missing-theme\n')
    monkeypatch.setattr(runtime.model_client, 'stream_model_async',
        lambda **kwargs: pytest.fail('provider reached'))
    assert cli.main(['-p', 'hello']) == 2
    error = capsys.readouterr().err
    assert 'missing-theme' in error
    assert all(str(root / 'themes') in error for root in paths.layers()[:2])


def test_parameter_prose_follows_the_selected_tool_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(paths.home() / 'tool_descriptions/fixture.md',
           'Home contract.\n<!--parameters-->\n### path\nHome parameter.\n')
    assert descriptions.load_description('fixture') == 'Home contract.'
    assert descriptions.load_parameter_description('fixture', 'path') == 'Home parameter.'
    _write(paths.home() / 'tool_descriptions/fixture.md', '')
    assert descriptions.load_parameter_description('fixture', 'path') == ''


def test_parameter_missing_value_names_its_source_file(tmp_path):
    path = _write(paths.home() / 'tool_descriptions/fixture.md',
                  'Contract.\n<!--parameters-->\n### path\n{{REQUIRED}}\n')
    with pytest.raises(ValueError) as error:
        descriptions.load_parameter_description('fixture', 'path')
    assert str(path) in str(error.value)
    assert 'REQUIRED' in str(error.value)


def test_invalid_tool_description_names_its_source_file(tmp_path):
    path = paths.home() / 'tool_descriptions/fixture.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\xff')
    with pytest.raises(ValueError) as error:
        descriptions.load_description('fixture')
    assert str(path) in str(error.value)
