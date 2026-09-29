"""Path-scoped skills: frontmatter ``paths:`` offers a skill when read, patch or
write first touches a matching file, once per session."""

import json
import re
from pathlib import Path

import pytest

from js import memory, runtime
from js import skills as skills_mod
from js.toolkit import ToolContext, fs
from js.toolkit.registry import build_default_registry

from test_lazy_tool_discovery import _cfg, _result

SELECTION = ['read:eager', 'write:eager', 'patch:eager', 'skill:lazy']


def _skill(root: Path, name: str, frontmatter: str) -> Path:
    path = root / '.js' / 'skills' / name / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: {name} rules.\n{frontmatter}---\nBody of {name}.\n')
    return path


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_mod, 'BUILTIN_SKILLS_DIR', tmp_path / 'no-builtin-skills')
    _skill(tmp_path, 'rust-style', "paths: ['*.rs']\n")
    _skill(tmp_path, 'plain', '')
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src' / 'main.rs').write_text('fn main() {}\n')
    (tmp_path / 'src' / 'lib.rs').write_text('pub fn f() {}\n')
    (tmp_path / 'notes.py').write_text('x = 1\n')
    return tmp_path


def _run(project, monkeypatch, cfg, responses, context=None):
    monkeypatch.setattr(runtime.model_client, 'stream_model_async', lambda **kw: next(responses))
    messages = [{'role': 'user', 'content': 'work'}]
    runtime.run_turn(cfg, 'system', messages, runtime.Telemetry(None),
                     tool_registry=build_default_registry().select(SELECTION),
                     tool_context=context or ToolContext(cwd=project))
    return [m['content'] for m in messages if m.get('role') == 'tool']


def _turn(project, monkeypatch, cfg, calls, context=None):
    """One turn: each call is its own model response, then a final answer."""
    responses = iter([*(_result((f'c{i}', name, json.dumps(args))) for i, (name, args) in enumerate(calls)),
                      _result(text='done')])
    return _run(project, monkeypatch, cfg, responses, context)


def _reminders(results):
    return [found for result in results for found in re.findall(r'<js-reminder>.*?</js-reminder>', result, re.S)]


def _metadata(paths):
    return skills_mod.SkillMetadata('s', 'd', (), 'project', Path('SKILL.md'), paths=tuple(paths))


@pytest.mark.parametrize(('paths', 'relative', 'expected'), [
    (['*.rs'], 'main.rs', True),
    (['*.rs'], 'src/deep/main.rs', True),
    (['*.rs'], 'src/main.py', False),
    (['*.rs'], '../outside/main.rs', False),
    (['src/**/*.ts'], 'src/a/b/c.ts', True),
    (['src/**/*.ts'], 'src/c.ts', True),
    (['src/**/*.ts'], 'lib/src/c.ts', False),
    (['/docs'], 'docs/guide/intro.md', True),
    (['/docs'], 'site/docs/intro.md', False),
    (['migrations/'], 'app/migrations/0001.sql', True),
    (['build/**'], 'build/out/x.o', True),
    (['build/**'], 'src/build/x.o', False),
])
def test_path_patterns_match_relative_to_working_directory(paths, relative, expected):
    assert _metadata(paths).matches_path(relative) is expected


def test_paths_frontmatter_accepts_a_list_or_a_comma_string(project):
    _skill(project, 'web', 'paths: "*.ts, *.tsx"\n')
    _skill(project, 'everything', "paths: ['**']\n")
    catalog = skills_mod.discover_skills(project)
    assert catalog.get('rust-style').paths == ('*.rs',)
    assert catalog.get('web').paths == ('*.ts', '*.tsx')
    assert catalog.get('everything').paths == ()
    assert catalog.get('plain').paths == ()


def test_paths_frontmatter_of_the_wrong_type_skips_the_skill(project):
    _skill(project, 'broken', 'paths: 3\n')
    assert skills_mod.discover_skills(project).get('broken') is None


def test_first_matching_touch_offers_the_skill_once(project, monkeypatch):
    results = _turn(project, monkeypatch, _cfg(project), [
        ('read', {'file_path': 'notes.py'}),
        ('read', {'file_path': 'src/main.rs'}),
        ('read', {'file_path': str(project / 'src' / 'lib.rs')}),
        ('patch', {'file_path': 'src/main.rs', 'old_string': 'main', 'new_string': 'start'}),
        ('write', {'file_path': 'src/new.rs', 'content': 'fn g() {}\n'}),
    ])
    assert _reminders(results[:1]) == []
    offered = _reminders(results[1:2])
    assert len(offered) == 1
    assert 'rust-style' in offered[0]
    assert 'tool_discovery {"load":"skill:rust-style"}' in offered[0]
    assert 'plain' not in offered[0]
    assert _reminders(results[2:]) == []
    assert (project / 'src' / 'main.rs').read_text() == 'fn start() {}\n'
    assert (project / 'src' / 'new.rs').exists()


def test_a_write_as_first_touch_offers_the_skill(project, monkeypatch):
    results = _turn(project, monkeypatch, _cfg(project), [
        ('write', {'file_path': 'src/fresh.rs', 'content': 'fn h() {}\n'}),
    ])
    offered = _reminders(results)
    assert len(offered) == 1 and 'rust-style' in offered[0]


def test_a_patch_as_first_touch_offers_the_skill(project, monkeypatch):
    # patch needs a prior read; a direct read outside any turn gives it one
    # without touching the session's record.
    context = ToolContext(cwd=project)
    fs.fs_read(file_path='src/main.rs', context=context)
    results = _turn(project, monkeypatch, _cfg(project), [
        ('patch', {'file_path': 'src/main.rs', 'old_string': 'main', 'new_string': 'start'}),
    ], context)
    assert not results[0].startswith('ERROR')
    offered = _reminders(results)
    assert len(offered) == 1 and 'rust-style' in offered[0]


def test_offer_is_once_per_session_across_turns_and_resume(project, monkeypatch):
    cfg = _cfg(project)
    first = _turn(project, monkeypatch, cfg, [('read', {'file_path': 'src/main.rs'})])
    assert len(_reminders(first)) == 1
    # A later turn in a fresh ToolContext, as a resumed process has, keeps the
    # session's record.
    again = _turn(project, monkeypatch, cfg, [('read', {'file_path': 'src/lib.rs'})])
    assert _reminders(again) == []
    # A reset starts a new conversation, which has not seen the offer.
    memory.append_mark(cfg.session_file, 'session_reset')
    after_reset = _turn(project, monkeypatch, cfg, [('read', {'file_path': 'src/lib.rs'})])
    assert len(_reminders(after_reset)) == 1


def test_a_loaded_skill_is_not_offered(project, monkeypatch):
    results = _turn(project, monkeypatch, _cfg(project), [
        ('tool_discovery', {'load': 'skill:rust-style'}),
        ('read', {'file_path': 'src/main.rs'}),
    ])
    assert 'Body of rust-style' in results[0]
    assert _reminders(results) == []


def test_failed_touch_offers_nothing(project, monkeypatch):
    results = _turn(project, monkeypatch, _cfg(project), [
        ('read', {'file_path': 'src/missing.rs'}),
        ('read', {'file_path': 'src/main.rs'}),
    ])
    assert results[0].startswith('ERROR')
    assert _reminders(results[:1]) == []
    assert len(_reminders(results[1:])) == 1


def test_parallel_reads_in_one_batch_offer_once(project, monkeypatch):
    responses = iter([
        _result(('a', 'read', '{"file_path":"src/main.rs"}'), ('b', 'read', '{"file_path":"src/lib.rs"}')),
        _result(text='done'),
    ])
    assert len(_reminders(_run(project, monkeypatch, _cfg(project), responses))) == 1
