from dataclasses import replace

import pytest

from js import persona, skills
from js import cli, verify
from types import SimpleNamespace
from js.config import Config
from js.cli_options import RunOptions, invocation


def test_prompt_file_bypasses_agent_and_instructions(tmp_path, monkeypatch):
    roots = tuple(tmp_path / name for name in ('repo', 'global', 'project'))
    default = roots[1] / 'defaultagent'
    default.mkdir(parents=True)
    (default / 'agent.yaml').write_text('tools: [fetch:eager]\n')
    (default / 'bad.md').write_text('NEVER LOAD THIS')
    prompt = tmp_path / 'prompt'
    prompt.write_text('Only {{PROMPT_WORD}}')
    monkeypatch.setenv('PROMPT_WORD', 'this')
    cfg = Config(model='test', prompt_roots=roots, prompts_dir=tmp_path / 'missing')
    with invocation(RunOptions(system_prompt_file=str(prompt))):
        spec = persona.load_configured_prompt_spec(cfg)
    assert spec.system == 'Only this'
    assert spec.tool_selectors == ('fetch:eager',)
    assert not spec.exchanges


@pytest.mark.parametrize('selection,expected', [
    ('fetch', ('fetch:eager',)),
    ('shell:eager,fetch:lazy', ('shell:eager', 'fetch:lazy')),
    ('web*, tag:read_only', ('web*:eager', 'tag:read_only')),
])
def test_tools_list_is_exactly_these_and_chat_suppresses_environment(selection, expected, monkeypatch):
    monkeypatch.delenv('JS_NOENV', raising=False)
    with invocation(RunOptions(chat=True, tools=selection)):
        import os
        assert os.environ['JS_NOENV'] == '1'
        assert RunOptions(chat=True, tools=selection).selectors(('read:eager',)) == expected
    assert 'JS_NOENV' not in os.environ
    assert RunOptions(tools=selection).selectors(('read:eager',)) == expected


@pytest.mark.parametrize('selection', ['shell:ban', 'shell,', '["shell:eager"]', ':lazy'])
def test_tools_list_refuses_what_it_cannot_read(selection):
    with pytest.raises(ValueError):
        RunOptions(tools=selection).selectors(())


def test_no_tools_starts_from_nothing_and_drops_mcp_and_skills():
    options = RunOptions(no_tools=True)
    assert options.selectors(('read:eager', 'shell:eager')) == ()
    assert options.skips_mcp and options.skips_skills


def _roots_with_agent(tmp_path, name='a', tools='tools: [fetch:eager]\n'):
    roots = tuple(tmp_path / part for part in ('repo', 'global', 'project'))
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    directory = roots[1] / name
    directory.mkdir()
    (directory / 'agent.yaml').write_text(tools)
    (directory / '01-prompt.md').write_text('PERSONA')
    (directory / '02-env.md').write_text('ENVIRONMENT')
    (directory / 'README.md').write_text('README')
    (directory / '01-user.md').write_text('SEED')
    default = roots[1] / 'defaultagent'
    if not default.exists():
        default.mkdir()
        (default / 'agent.yaml').write_text('tools: [shell:eager]\n')
        (default / '01-prompt.md').write_text('DEFAULT PERSONA')
    return roots


def test_you_are_is_identity_text_and_appends_with_zero_tools(tmp_path):
    roots = _roots_with_agent(tmp_path, 'defaultagent')
    instructions = tmp_path / 'AGENTS.md'
    instructions.write_text('PROJECT RULES')
    cfg = Config(model='before', agent_id='defaultagent', prompt_roots=roots, agents_files=(instructions,))
    with invocation(RunOptions(you_are='Answer in one word.', append_system_prompt=('tail',))):
        spec = persona.load_configured_prompt_spec(cfg, expand=False)
        spec = persona._expand_spec(spec, replace(cfg, model='cpa/hyper/glm-5.3'))
    assert spec.system == 'You are glm-5.3 running inside the js agent\nAnswer in one word.\n\ntail'
    assert spec.tool_selectors == ()
    assert not spec.exchanges


def test_you_are_without_text_runs_no_envctx(tmp_path, monkeypatch):
    monkeypatch.delenv('JS_NOENV', raising=False)
    roots = _roots_with_agent(tmp_path, 'defaultagent')
    cfg = Config(model='m', agent_id='defaultagent', prompt_roots=roots)
    with invocation(RunOptions(you_are='')):
        import os
        assert os.environ['JS_NOENV'] == '1'
        spec = persona.load_configured_prompt_spec(cfg)
    assert spec.system == 'You are m running inside the js agent'
    options = RunOptions(you_are='')
    assert options.skips_mcp and options.skips_skills and options.skips_agents_md


def test_you_are_takes_tools_only_from_the_tools_flag(tmp_path):
    roots = _roots_with_agent(tmp_path, 'defaultagent')
    cfg = Config(model='m', agent_id='defaultagent', prompt_roots=roots)
    with invocation(RunOptions(you_are='', tools='read')):
        spec = persona.load_configured_prompt_spec(cfg, expand=False)
    assert spec.tool_selectors == ('read:eager',)


def test_system_prompt_replaces_only_the_prompt_files(tmp_path):
    roots = _roots_with_agent(tmp_path)
    instructions = tmp_path / 'AGENTS.md'
    instructions.write_text('PROJECT RULES')
    cfg = Config(model='m', agent_id='a', prompt_roots=roots, agents_files=(instructions,))
    with invocation(RunOptions(system_prompt='REPLACED')):
        spec = persona.load_configured_prompt_spec(cfg, expand=False)
    assert spec.system == 'PROJECT RULES\n\nREPLACED\n\nENVIRONMENT\n'
    assert spec.tool_selectors == ('fetch:eager',)
    assert spec.exchanges


def test_no_env_leaves_out_env_files(tmp_path):
    roots = _roots_with_agent(tmp_path)
    cfg = Config(model='m', agent_id='a', prompt_roots=roots)
    with invocation(RunOptions(no_env=True)):
        spec = persona.load_configured_prompt_spec(cfg, expand=False)
    assert spec.system == 'PERSONA\n'
    assert spec.tool_selectors == ('fetch:eager',)


def test_chat_with_agent_loads_only_its_prompt_files(tmp_path):
    roots = _roots_with_agent(tmp_path)
    instructions = tmp_path / 'AGENTS.md'
    instructions.write_text('PROJECT RULES')
    cfg = Config(model='m', agent_id='a', prompt_roots=roots, agents_files=(instructions,))
    with invocation(RunOptions(chat=True, agent='a')):
        spec = persona.load_configured_prompt_spec(cfg, expand=False)
    assert spec.system == 'PERSONA\n'
    assert spec.tool_selectors == ()
    assert not spec.exchanges


def test_chat_does_not_load_any_instruction_or_agent_file(tmp_path):
    cfg = Config(model='test', prompts_dir=tmp_path / 'missing', agents_files=(tmp_path / 'missing-instructions',))
    with invocation(RunOptions(chat=True)):
        spec = persona.load_configured_prompt_spec(cfg)
    assert spec.system == ''
    assert spec.tool_selectors == ()
    assert not spec.exchanges


def test_no_skills_removes_access_and_skips_discovery(tmp_path, monkeypatch):
    def unexpected(*args):
        pytest.fail('skill directories were scanned')
    monkeypatch.setattr(skills, '_skill_paths', unexpected)
    with invocation(RunOptions(no_skills=True)):
        assert not skills.discover_skills(tmp_path).skills
        assert RunOptions(no_skills=True).selectors(('skill:eager',)) == ('skill:ban', 'skill:eager')


def test_verify_reports_bad_manifest_without_executing_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(verify.paths, 'home', lambda: tmp_path)
    monkeypatch.setattr(verify.paths, 'global_agents_dir', lambda: tmp_path / 'agents')
    directory = tmp_path / 'agents' / 'broken'
    directory.mkdir(parents=True)
    (directory / 'agent.yaml').write_text('tools: invalid')
    (directory / 'prompt.md').write_text('!{sh exit 99}')
    errors = verify.verify('broken')
    assert errors and str(directory) in errors[0]


def test_bench_models_visits_each_model_and_continues_after_error(monkeypatch):
    monkeypatch.setattr(cli, '_from_env', lambda *a, **kw: Config(model='test'))
    monkeypatch.setattr(cli, '_list_models_provider_ids', lambda *a: ['p'])
    monkeypatch.setattr(cli, '_models_cached_or_live', lambda *a: ['bad', 'good'])
    visited = []
    def run(prompt, **kw):
        visited.append(kw['model'])
        if kw['model'] == 'p/bad':
            raise ValueError('unknown model')
        return 0
    monkeypatch.setattr(cli, '_run_prompt', run)
    args = SimpleNamespace(extras=[], agent=None, no_local_config=False, no_global_config=False,
                           reasoning=None, max_out=None, stats_json=None, stats_csv=None)
    assert cli._run_model_bench(args, []) == 1
    assert visited == ['p/bad', 'p/good']


def test_verify_checks_the_agent_roots_only(tmp_path, monkeypatch):
    monkeypatch.setattr(verify.paths, 'home', lambda: tmp_path)
    monkeypatch.setattr(verify.paths, 'global_agents_dir', lambda: tmp_path / 'agents')
    monkeypatch.chdir(tmp_path)
    for elsewhere in (tmp_path / 'worktrees' / 'js-x' / 'prompts' / 'stale',
                      tmp_path / 'scratch' / 'old'):
        elsewhere.mkdir(parents=True)
        (elsewhere / 'agent.yaml').write_text('tools: invalid')
        (elsewhere / 'agents.yaml').write_text('tools: invalid')
    assert not [error for error in verify.verify() if str(tmp_path / 'worktrees') in error
                or str(tmp_path / 'scratch') in error]
