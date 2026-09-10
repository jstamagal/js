import asyncio
import json

import pytest

from js.toolkit.core import CatalogEntry
from js.toolkit.registry import build_default_registry


@pytest.mark.parametrize('filters', [{}, {'kind': 'skill'}, {'source': 'global'}])
def test_browse_is_bounded_and_pages_all_entries(tmp_path, monkeypatch, filters):
    surface = build_default_registry().select(['read']).lazy_surface(tmp_path)
    entries = tuple(CatalogEntry(f'skill:item-{i:03}', f'item-{i:03}', '長' * 5000, 'skill', 'global') for i in range(120))
    monkeypatch.setattr(surface, 'catalog', lambda: entries)
    seen = []
    offset = 0
    while True:
        raw = surface.discover(**filters, **({'offset': offset} if offset else {}))
        assert len(raw.encode()) <= 4096
        page = json.loads(raw)
        assert all('description' not in item for item in page['results'])
        seen.extend(item['id'] for item in page['results'])
        if not page['truncated']:
            break
        offset = page['next_offset']
    assert seen == [entry.id for entry in entries]


@pytest.mark.parametrize(('query', 'expected'), [('download', 'fetch'), ('screenshot', 'terminal_snapshot'), ('jupyter', 'kernel'), ('edit file', 'patch'), ('find files', 'fs_search'), ('spawn agent', 'task')])
def test_intent_search_ranks_native_tools(tmp_path, query, expected):
    surface = build_default_registry().lazy_surface(tmp_path)
    results = json.loads(surface.discover(query=query))['results']
    assert results[0]['id'] == f'native:{expected}'


def test_word_boundaries_and_partial_match_ranking(tmp_path, monkeypatch):
    surface = build_default_registry().select(['read']).lazy_surface(tmp_path)
    entries = (CatalogEntry('skill:skills', 'skills', 'Skill instructions', 'skill', 'global'), CatalogEntry('native:kill', 'kill', 'Stop process', 'native', 'shell'))
    monkeypatch.setattr(surface, 'catalog', lambda: entries)
    assert [item['id'] for item in json.loads(surface.discover(query='kill process unknown'))['results']] == ['native:kill']


def test_async_mcp_results_are_bounded(tmp_path):
    class Host:
        def reserve_public_names(self, names):
            pass

        async def discover(self, **kwargs):
            return tuple(CatalogEntry(f'mcp:tool-{i}', f'tool-{i}', 'search ' + '長' * 5000, 'mcp', 'server') for i in range(100))

    surface = build_default_registry().select(['read']).lazy_surface(tmp_path, mcp_host=Host())
    raw = asyncio.run(surface.discover_async(kind='mcp', query='search'))
    assert len(raw.encode()) <= 8192
    assert json.loads(raw)['truncated']


@pytest.mark.parametrize('offset', [-1, True, '1', 1.5])
def test_invalid_offset_is_rejected(tmp_path, offset):
    surface = build_default_registry().select(['read']).lazy_surface(tmp_path)
    assert surface.discover(offset=offset).startswith('ERROR: offset')


def test_oversized_identifier_is_reported_without_corrupting_load_id(tmp_path, monkeypatch):
    surface = build_default_registry().select(['read']).lazy_surface(tmp_path)
    entries = (CatalogEntry('skill:' + '長' * 5000, 'large', '', 'skill', 'global'), CatalogEntry('skill:ok', 'ok', '', 'skill', 'global'))
    monkeypatch.setattr(surface, 'catalog', lambda: entries)
    raw = surface.discover()
    assert len(raw.encode()) <= 4096
    page = json.loads(raw)
    assert page['omitted_oversized_entries'] == 1
    assert [item['id'] for item in page['results']] == ['skill:ok']
    assert not page['truncated']


def test_empty_search_provides_browse_hint(tmp_path):
    surface = build_default_registry().select(['read']).lazy_surface(tmp_path)
    page = json.loads(surface.discover(query='zqxnonexistent'))
    assert page['results'] == []
    assert 'empty query' in page['hint']


def test_mcp_host_token_matching_preserves_partial_results(tmp_path):
    from js.mcp.host import MCPHost
    from js.mcp_config import MCPConfiguration, MCPPolicy, MCPServer

    class Client:
        def __init__(self, factory, **kwargs):
            self.initialized = False

        async def initialize(self, **kwargs):
            self.initialized = True

        async def list_tools(self, **kwargs):
            return [{'name': 'kill_process', 'description': 'Stop a process', 'inputSchema': {}},
                    {'name': 'skills', 'description': 'Skill catalog', 'inputSchema': {}}]

    async def drive():
        server = MCPServer('Tools', 'tools', 'stdio', command='unused')
        host = MCPHost(MCPConfiguration((server,), MCPPolicy()), client_factory=Client)
        surface = build_default_registry().select([]).lazy_surface(tmp_path, mcp_host=host)
        results = json.loads(await surface.discover_async(query='kill process unknown', kind='mcp'))['results']
        assert [item['id'] for item in results] == ['mcp:tools__kill_process']

    asyncio.run(drive())
