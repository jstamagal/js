"""BM25 discovery ranking on a fixed catalog, and the load hint for unloaded tools."""

import asyncio
import json
import re

import pytest

from js import runtime
from js.toolkit import ToolContext
from js.toolkit.core import CatalogEntry, Tool
from js.toolkit.discovery import ranked_entries
from js.toolkit.registry import ToolRegistry, build_default_registry

from test_lazy_tool_discovery import _cfg, _result


def _entry(name, description='', *, search_text='', kind='mcp', source='fixture'):
    return CatalogEntry(f'{kind}:{name}', name, description, kind, source, search_text=search_text)


CATALOG = (
    _entry('terminal_session', 'Drive a long-lived interactive process.'),
    _entry('terminal_snapshot',
           'Render the terminal as an image. Works in any terminal session; a terminal '
           'session with many terminal lines renders the terminal session screen.'),
    _entry('browserProbe', 'Inspect a web page.'),
    _entry('list_issues', 'List issues in a repository.'),
    _entry('list_gists', 'List gists of a user.'),
    _entry('list_branches', 'List branches in a repository.'),
    _entry('list_tags', 'List tags in a repository.'),
    _entry('deploy', 'Ship a build.',
           search_text='environment Target environment such as staging or production'),
    _entry('alpha', 'Shared word.'),
    _entry('beta', 'Shared word.'),
)


def _ranked(query):
    return [item.name for item in ranked_entries(CATALOG, query)]


def test_name_match_outranks_repeated_description_words():
    assert _ranked('terminal session')[:2] == ['terminal_session', 'terminal_snapshot']


def test_camel_case_name_splits_into_words():
    assert _ranked('probe') == ['browserProbe']


def test_camel_case_query_word_matches_the_word_written_whole():
    catalog = (
        _entry('create_pr', 'Open a pull request.', source='github'),
        _entry('create_mr', 'Open a merge request.', source='gitlab'),
    )
    assert [item.name for item in ranked_entries(catalog, 'GitHub')][0] == 'create_pr'


def test_rare_term_outweighs_common_term():
    # 'repository' is in three descriptions, 'user' in one.
    assert _ranked('repository user')[0] == 'list_gists'


def test_plural_query_reaches_singular_name():
    assert _ranked('branch')[0] == 'list_branches'


def test_schema_text_is_searched():
    assert _ranked('staging') == ['deploy']


def test_ties_order_by_id_and_misses_are_dropped():
    assert _ranked('shared word zzz') == ['alpha', 'beta']


def test_empty_query_lists_everything_by_id():
    assert _ranked('') == sorted(item.name for item in CATALOG)


def test_stop_word_query_returns_nothing():
    assert _ranked('the of a') == []


def test_native_schema_property_names_are_searched(tmp_path):
    async def handler(**kwargs):
        return 'ok'

    tool = Tool('frob', 'Adjust the widget.', handler,
                {'zebra_count': {'type': 'integer', 'description': 'How many quokkas to add.'}})
    registry = ToolRegistry(tools=(tool,), aliases={'frob': 'frob'}, lazy=frozenset({'frob'}))
    surface = registry.lazy_surface(tmp_path)
    for query in ('zebra', 'quokka'):
        assert [row['id'] for row in json.loads(surface.discover(query=query))['results']] == ['native:frob']


def test_mcp_input_schema_is_searched(tmp_path):
    from js.mcp.host import MCPHost
    from js.mcp_config import MCPConfiguration, MCPPolicy, MCPServer

    class Client:
        def __init__(self, factory, **kwargs):
            self.initialized = False

        async def initialize(self, **kwargs):
            self.initialized = True

        async def list_tools(self, **kwargs):
            return [
                {'name': 'ship', 'description': 'Ship it',
                 'inputSchema': {'type': 'object', 'properties': {
                     'region': {'type': 'string', 'description': 'Datacenter such as frankfurt'}}}},
                {'name': 'other', 'description': 'Something else', 'inputSchema': {}},
            ]

    async def drive():
        server = MCPServer('Ops', 'ops', 'stdio', command='unused')
        host = MCPHost(MCPConfiguration((server,), MCPPolicy()), client_factory=Client)
        surface = build_default_registry().select([]).lazy_surface(tmp_path, mcp_host=host)
        results = json.loads(await surface.discover_async(query='frankfurt', kind='mcp'))['results']
        assert [row['id'] for row in results] == ['mcp:ops__ship']
        error = surface.dispatch_registry().unavailable_error('ops__ship')
        assert error.startswith('ERROR')
        assert 'tool_discovery {"load":"mcp:ops__ship"}' in error

    asyncio.run(drive())


@pytest.mark.parametrize('called', ['shell', 'Run'])
def test_unloaded_tool_error_names_the_load_call_that_fixes_it(tmp_path, monkeypatch, called):
    messages = [{'role': 'user', 'content': 'run'}]
    seen_tools = []

    def load_call_from_last_error():
        error = messages[-1]['content']
        assert error.startswith('ERROR')
        return re.search(r'tool_discovery (\{.*?\})', error).group(1)

    steps = iter([
        lambda: _result(('first', called, '{"command":"printf hi"}')),
        lambda: _result(('load', 'tool_discovery', load_call_from_last_error())),
        lambda: _result(('again', called, '{"command":"printf hi"}')),
        lambda: _result(text='done'),
    ])

    def stream(**kwargs):
        seen_tools.append([tool.name for tool in kwargs['tools']])
        return next(steps)()

    monkeypatch.setattr(runtime.model_client, 'stream_model_async', stream)
    registry = build_default_registry().select(['shell:lazy'])
    if called == 'Run':
        registry = registry.aliased({'shell': 'Run'})
    runtime.run_turn(_cfg(tmp_path), 'system', messages, runtime.Telemetry(None),
                     tool_registry=registry, tool_context=ToolContext(cwd=tmp_path))
    results = {m['tool_call_id']: m['content'] for m in messages if m.get('role') == 'tool'}
    assert json.loads(results['load'])['id'] == 'native:shell'
    assert 'hi' in results['again']
    assert 'shell' not in seen_tools[0]
    assert 'shell' in seen_tools[2]
