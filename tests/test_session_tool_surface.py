"""Session lifecycle and recovery for the lazy tool surface."""
from __future__ import annotations

import json

from js import memory, runtime
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry
from test_lazy_tool_discovery import _cfg, _result


def test_loaded_tools_survive_next_turn_and_fresh_context(tmp_path, monkeypatch):
    emitted = []
    results = iter([
        _result(('load', 'tool_discovery', '{"load":"native:shell"}')),
        _result(text='ready'),
        _result(('run', 'shell', '{"command":"printf retained"}')),
        _result(text='done'),
    ])
    def stream(**kwargs):
        emitted.append([t.name for t in kwargs['tools']])
        return next(results)
    monkeypatch.setattr(runtime.model_client, 'stream_model_async', stream)
    cfg = _cfg(tmp_path)
    registry = build_default_registry().select(['shell'])
    for prompt in ('load it', 'use it'):
        messages = [{'role': 'user', 'content': prompt}]
        runtime.run_turn(cfg, 'system', messages, runtime.Telemetry(None),
                         tool_registry=registry, tool_context=ToolContext(cwd=tmp_path))
    assert 'shell' not in emitted[0]
    assert 'shell' in emitted[2]
    assert any(m.get('role') == 'tool' and 'retained' in m['content'] for m in messages)


def test_unloaded_calls_explain_recovery_without_spending_retry_budget(monkeypatch, tmp_path):
    results = iter([
        _result(*[(f'c{i}', 'shell', json.dumps({'command': f'echo {i}'})) for i in range(3)]),
        _result(text='recovered'),
    ])
    monkeypatch.setattr(runtime.model_client, 'stream_model_async', lambda **kw: next(results))
    messages = [{'role': 'user', 'content': 'run'}]
    runtime.run_turn(_cfg(tmp_path), 'system', messages, runtime.Telemetry(None),
                     tool_registry=build_default_registry().select(['shell']),
                     tool_context=ToolContext(cwd=tmp_path))
    errors = [m['content'] for m in messages if m.get('role') == 'tool']
    assert len(errors) == 3
    assert all('native:shell' in e and '<retry>' not in e and 'invalid arguments' not in e for e in errors)
    assert messages[-1]['content'] == 'recovered'


def test_tool_surface_reset_and_policy_reduction(tmp_path, monkeypatch):
    emitted = []
    responses = iter([_result(('load', 'tool_discovery', '{"load":"native:shell"}')),
                      _result(text='done'), _result(text='reduced'), _result(text='reset')])
    def stream(**kwargs):
        emitted.append([t.name for t in kwargs['tools']])
        return next(responses)
    monkeypatch.setattr(runtime.model_client, 'stream_model_async', stream)
    cfg = _cfg(tmp_path)
    for selectors in (['shell'], ['read']):
        runtime.run_turn(cfg, 'system', [{'role': 'user', 'content': 'hi'}], runtime.Telemetry(None),
                         tool_registry=build_default_registry().select(selectors),
                         tool_context=ToolContext(cwd=tmp_path))
    assert 'shell' not in emitted[2]
    memory.append_mark(cfg.session_file, 'session_reset')
    runtime.run_turn(cfg, 'system', [{'role': 'user', 'content': 'hi'}], runtime.Telemetry(None),
                     tool_registry=build_default_registry().select(['shell']),
                     tool_context=ToolContext(cwd=tmp_path))
    assert 'shell' not in emitted[3]


def test_sessionless_loading_does_not_fsync_devnull(tmp_path, monkeypatch):
    from dataclasses import replace
    from pathlib import Path
    results = iter([_result(('load', 'tool_discovery', '{"load":"native:shell"}')), _result(text='done')])
    monkeypatch.setattr(runtime.model_client, 'stream_model_async', lambda **kw: next(results))
    messages = [{'role': 'user', 'content': 'load'}]
    runtime.run_turn(replace(_cfg(tmp_path), session_file=Path('/dev/null')), 'system', messages,
                     runtime.Telemetry(None), tool_registry=build_default_registry().select(['shell']),
                     tool_context=ToolContext(cwd=tmp_path))
    result = next(m['content'] for m in messages if m.get('role') == 'tool')
    assert json.loads(result)['loaded'] == ['shell']


def test_surface_marks_survive_compaction_but_not_reset(tmp_path):
    path = tmp_path / 'session.jsonl'
    snapshot = {'version': 1, 'ids': ['native:shell']}
    memory.append_tool_surface(path, snapshot)
    memory.append_mark(path, 'compaction:' + json.dumps({'summary': 'summary', 'keep_from': 0}))
    assert memory.load_tool_surface(path) == snapshot
    assert memory.load_messages(path) == [{'role': 'user', 'content': '<compaction-summary>\nsummary\n</compaction-summary>'}]
    memory.append_mark(path, 'session_reset')
    assert memory.load_tool_surface(path) is None


def test_unavailable_errors_distinguish_denied_unknown_and_alias(tmp_path):
    surface = build_default_registry().select(['shell']).aliased({'shell': 'Run'}).lazy_surface(tmp_path)
    frozen = surface.dispatch_registry()
    for registry in (surface, frozen):
        for name, expected in [('Run', 'native:shell'), ('read', 'not allowed'), ('imaginary', 'unknown tool')]:
            tracker = runtime.ToolErrorTracker()
            _, error = runtime._dispatch(name, '{}', runtime.Telemetry(None), 4096,
                                         registry=registry, tool_context=ToolContext(cwd=tmp_path),
                                         error_tracker=tracker)
            assert expected in error
            assert '<retry>' not in error
            assert not tracker.limit_reached()


def test_load_does_not_authorize_sibling_and_next_call_can_recover(tmp_path, monkeypatch):
    responses = iter([
        _result(('load', 'tool_discovery', '{"load":"native:shell"}'),
                ('early', 'shell', '{"command":"printf early"}')),
        _result(('later', 'shell', '{"command":"printf recovered"}')),
        _result(text='done'),
    ])
    monkeypatch.setattr(runtime.model_client, 'stream_model_async', lambda **kw: next(responses))
    messages = [{'role': 'user', 'content': 'run'}]
    runtime.run_turn(_cfg(tmp_path), 'system', messages, runtime.Telemetry(None),
                     tool_registry=build_default_registry().select(['shell']), tool_context=ToolContext(cwd=tmp_path))
    results = {m['tool_call_id']: m['content'] for m in messages if m.get('role') == 'tool'}
    assert 'same response' in results['early']
    assert '<retry>' not in results['early']
    assert 'recovered' in results['later']


def test_restored_native_tools_obey_new_alias_profile(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, {'tools': {'alias_profiles': [{'match': ['offline'], 'aliases': {'shell': 'Run'}}]}})
    memory.append_tool_surface(cfg.session_file, {'version': 1, 'agent_id': cfg.agent_id,
        'cwd': str(tmp_path.resolve()), 'ids': ['native:shell'], 'mcp_sources': []})
    seen = []
    def stream(**kwargs):
        seen.extend(tool.name for tool in kwargs['tools'])
        return _result(text='ready')
    monkeypatch.setattr(runtime.model_client, 'stream_model_async', stream)
    runtime.run_turn(cfg, 'system', [{'role': 'user', 'content': 'hi'}], runtime.Telemetry(None),
                     tool_registry=build_default_registry().select(['shell']), tool_context=ToolContext(cwd=tmp_path))
    assert seen == ['Run', 'tool_discovery']


def test_cancelled_mcp_restore_closes_owned_host(tmp_path, monkeypatch):
    import asyncio
    from dataclasses import replace
    from js.mcp import host as host_module
    from js.mcp_config import MCPConfiguration, MCPPolicy, MCPServer
    server = MCPServer('example', 'example', 'stdio', command='unused')
    cfg = replace(_cfg(tmp_path), mcp=MCPConfiguration((server,), MCPPolicy()))
    memory.append_tool_surface(cfg.session_file, {'version': 1, 'agent_id': cfg.agent_id,
        'cwd': str(tmp_path.resolve()), 'ids': ['mcp:example__tool'], 'mcp_sources': ['example']})
    host = host_module.MCPHost(cfg.mcp)
    closed = []
    async def cancelled(**kw):
        raise asyncio.CancelledError
    async def close():
        closed.append(True)
    monkeypatch.setattr(host, 'discover', cancelled)
    monkeypatch.setattr(host, 'close', close)
    monkeypatch.setattr(host_module, 'MCPHost', lambda *a, **kw: host)
    async def run():
        try:
            await runtime.run_turn_async(cfg, 'system', [{'role': 'user', 'content': 'hi'}], runtime.Telemetry(None),
                tool_registry=build_default_registry().select(['shell']), tool_context=ToolContext(cwd=tmp_path))
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('cancellation must propagate')
    asyncio.run(run())
    assert closed == [True]


def test_mcp_restore_reconnects_loaded_sources_and_obeys_policy(tmp_path):
    import asyncio
    from js.mcp.host import MCPHost
    from js.mcp_config import MCPConfiguration, MCPPolicy, MCPServer
    from test_mcp_runtime import FakeClient
    servers = (MCPServer('Alpha', 'alpha', 'stdio', command='fake'),
               MCPServer('Unused', 'unused', 'stdio', command='fake'))
    async def run():
        host = MCPHost(MCPConfiguration(servers, MCPPolicy()), client_factory=FakeClient)
        surface = build_default_registry().select(['shell']).lazy_surface(tmp_path, mcp_host=host)
        await surface.discover_async(source='Alpha')
        name = sorted(host.remote_tools)[0]
        surface.discover(load=f'mcp:{name}')
        state = surface.snapshot()
        await host.close()
        replacement = MCPHost(MCPConfiguration(servers, MCPPolicy()), client_factory=FakeClient)
        resumed = build_default_registry().select(['shell']).lazy_surface(tmp_path, mcp_host=replacement)
        await resumed.restore(state)
        assert resumed.resolve(name) is not None
        assert set(replacement.clients) == {'Alpha'}
        await replacement.close()
        denied = MCPHost(MCPConfiguration(servers, MCPPolicy(server_deny=('Alpha',))), client_factory=FakeClient)
        restricted = build_default_registry().select(['shell']).lazy_surface(tmp_path, mcp_host=denied)
        await restricted.restore(state)
        assert restricted.resolve(name) is None
        assert not denied.clients
        await denied.close()
    asyncio.run(run())


def test_every_request_trace_lists_published_names():
    import io
    from js import model_client
    tools = model_client.tool_specs_to_ai_tools(build_default_registry().select(['shell']).openai_specs())
    for schemas in (True, False):
        sink = io.StringIO()
        model_client._emit_request_trace(sink=sink, model_id='offline', provider_id=None,
            provider_base_url=None, params=None, messages=[], tools=tools,
            dump_schemas=schemas, dump_from=0)
        output = sink.getvalue()
        header, _ = json.JSONDecoder().raw_decode(output[output.index("{"):])
        assert header["tool_names"] == ["shell"]


def test_skill_restore_retains_activated_native_tools_without_rereading_body(tmp_path):
    import asyncio
    from test_lazy_tool_discovery import _skill_file
    path = _skill_file(tmp_path / '.agents' / 'skills', 'inspect')
    path.write_text('---\ndescription: Inspect\ntools:\n  - shell\n---\nDo inspection.\n')
    registry = build_default_registry().select(['skill', 'shell'])
    surface = registry.lazy_surface(tmp_path)
    surface.discover(load='skill:inspect')
    state = surface.snapshot()
    assert 'native:shell' in state['ids']
    resumed = registry.lazy_surface(tmp_path)
    path.unlink()
    asyncio.run(resumed.restore(state))
    assert resumed.resolve('shell') is not None
    assert 'skill:inspect' in resumed.snapshot()['ids']
