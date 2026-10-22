from dataclasses import replace

import pytest

from js import persona, skills
from js import cli, model_health, verify
import asyncio
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
])
def test_chat_with_tools_is_exact_and_suppresses_environment(selection, expected, monkeypatch):
    monkeypatch.delenv('JS_NOENV', raising=False)
    with invocation(RunOptions(chat=True, with_tools=selection)):
        import os
        assert os.environ['JS_NOENV'] == '1'
        assert RunOptions(chat=True, with_tools=selection).selectors(('read:eager',)) == expected
    assert 'JS_NOENV' not in os.environ


def test_without_tools_requires_names_and_overrides_selection():
    options = RunOptions(with_tools='shell,fetch', without_tools='fetch')
    assert options.selectors(()) == ('fetch:ban', 'shell:eager', 'fetch:eager')


def test_identity_uses_resolved_model_and_append_expands_once(tmp_path):
    manifest = tmp_path / 'defaultagent'
    manifest.mkdir()
    (manifest / 'agent.yaml').write_text('tools: []')
    cfg = Config(model='before', prompt_roots=(tmp_path, tmp_path, tmp_path))
    with invocation(RunOptions(you_are=True, noenv=True, append_system_prompt=('tail',))):
        spec = persona.load_configured_prompt_spec(cfg, expand=False)
        spec = persona._expand_spec(spec, replace(cfg, model='cpa/hyper/glm-5.3'))
    assert spec.system == 'You are glm-5.3 running inside the js agent\n\ntail'


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


def test_health_waits_for_failure_deadline_and_ignores_immediate_errors(monkeypatch):
    removed = []
    monkeypatch.setattr(model_health.logins, 'remove_cached_model', lambda *key: removed.append(key))

    async def exercise():
        session = model_health.HealthSession(model_health.RemovalPolicy(failure=True, slow_ttft=0.01))
        async def invalid():
            model_health.sent()
            raise ValueError('unknown model')
        with pytest.raises(ValueError):
            await model_health.HealthCall(session, 'p', 'invalid').run(invalid())
        async def silent():
            model_health.sent()
            await asyncio.sleep(1)
        with pytest.raises(TimeoutError):
            await model_health.HealthCall(session, 'p', 'silent').run(silent())
        session.finish()
    asyncio.run(exercise())
    assert removed == [('p', 'silent')]


def test_health_aggregates_decode_and_requires_received_tokens(monkeypatch):
    removed = []
    monkeypatch.setattr(model_health.logins, 'remove_cached_model', lambda *key: removed.append(key))
    session = model_health.HealthSession(model_health.RemovalPolicy.parse('slow_decode:10'))
    session.models[('p', 'fast')] = model_health.Measurement(tokens=100, decode_s=5, received=True)
    session.models[('p', 'slow')] = model_health.Measurement(tokens=100, decode_s=20, received=True)
    session.models[('p', 'unknown')] = model_health.Measurement(tokens=100, decode_s=20)
    session.finish()
    assert removed == [('p', 'slow')]


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
    args = SimpleNamespace(extras=[], agent=None, ignore_local=False, ignore_global=False,
                           reasoning=None, max_out=None, stats_json=None, stats_csv=None)
    assert cli._run_model_bench(args, []) == 1
    assert visited == ['p/bad', 'p/good']
