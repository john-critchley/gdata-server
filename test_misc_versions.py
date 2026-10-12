"""Offline behavioral tests for the checkout and its HEAD Misc implementation.

External services and optional imports are replaced before either module loads.
HEAD exercises the monitoring/WebDAV functionality awaiting reconciliation.
"""
import asyncio
import contextlib
import datetime
import importlib.util
import json
import logging
from pathlib import Path
import subprocess
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent


def run(coro):
    return asyncio.run(coro)


class ToolServer:
    def __init__(self, name, **kwargs):
        self.name = name
        self.version = kwargs.get('version')
    def list_tools(self):
        def decorate(fn):
            self.list = fn
            return fn
        return decorate
    def call_tool(self):
        def decorate(fn):
            self.call = fn
            return fn
        return decorate


@pytest.fixture(params=['head', 'working'])
def server(request, monkeypatch):
    oauth = types.ModuleType('gdata_oauth')
    from fastapi import APIRouter
    oauth.router = APIRouter()
    oauth.BearerMiddleware = lambda app: app
    oauth.startup_check = MagicMock()
    plugins = types.ModuleType('mcp_tool_plugins')
    plugins.discover_tools = lambda: []
    imap = types.ModuleType('imap_client')
    imap.IMAPClient = MagicMock()
    pg = types.ModuleType('asyncpg')
    pg.Pool = object
    pg.Record = dict
    pg.create_pool = MagicMock()
    for name, mod in [('gdata_oauth', oauth), ('mcp_tool_plugins', plugins),
                      ('imap_client', imap), ('asyncpg', pg)]:
        monkeypatch.setitem(sys.modules, name, mod)
    if request.param == 'head':
        source = subprocess.check_output(['git', '-C', str(ROOT), 'show',
                                          'HEAD:misc_mcp_server.py'], text=True)
    else:
        source = (ROOT / 'misc_mcp_server.py').read_text()
    mod = types.ModuleType('misc_under_test_' + request.param)
    mod.__file__ = str(ROOT / 'misc_mcp_server.py')
    exec(compile(source, mod.__file__, 'exec'), mod.__dict__)
    mod.Server = ToolServer
    mod.variant = request.param
    return mod


def only(server, feature):
    if not hasattr(server, feature):
        pytest.skip(f'{feature} belongs to the other implementation')


def pool_with(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    return pool


@pytest.mark.parametrize('value,expected', [('true', True), ('YES', True), ('1', True),
                                          ('false', False), ('no', False), ('0', False)])
def test_environment_boolean(server, monkeypatch, value, expected):
    monkeypatch.setenv('TEST_MISC_BOOL', value)
    assert server._env_bool('TEST_MISC_BOOL', not expected) is expected


def test_environment_boolean_default(server, monkeypatch):
    monkeypatch.delenv('TEST_MISC_BOOL', raising=False)
    assert server._env_bool('TEST_MISC_BOOL', True)
    monkeypatch.setenv('TEST_MISC_BOOL', 'unknown')
    assert not server._env_bool('TEST_MISC_BOOL', False)


def test_row_serialisation(server):
    row = server._row_to_dict({'when': datetime.datetime(2026, 1, 2, 3, 4),
                               'date': datetime.date(2026, 1, 2), 'value': 7, 'null': None})
    assert row == {'when': '2026-01-02T03:04:00', 'date': '2026-01-02',
                   'value': 7, 'null': None}


def test_tools_and_dispatch(server):
    tools = server._make_tool_server('unit')
    listed = run(tools.list())
    names = {t.name for t in listed}
    assert {'hello', 'pg_query'} <= names
    expected = {'check', 'webdav'} if server.variant == 'head' else {
        'email_send', 'email_list', 'email_read', 'email_list_folders'}
    assert expected <= names
    assert all(t.description.startswith('[unit store] ') for t in listed)
    assert run(tools.call('hello', {}))[0].text == 'hello'
    assert 'unknown tool' in run(tools.call('does-not-exist', {}))[0].text
    assert 'required' in run(tools.call('pg_query', {'sql': ' '}))[0].text


@pytest.mark.parametrize('rows', [[{'value': 42}], []])
def test_database_results(server, rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    statement = MagicMock()
    statement.get_attributes.return_value = ('value',)
    statement.fetch = AsyncMock(return_value=rows)
    conn.prepare = AsyncMock(return_value=statement)
    conn.execute = AsyncMock()
    pool = pool_with(conn)
    if server.variant == 'head':
        server._pool = pool
    else:
        server._pools = {'owntracks_ro': pool}
    assert json.loads(run(server._pg_query('SELECT 42 AS value'))) == rows
    conn.execute.assert_not_awaited()


def test_database_missing_pool(server):
    assert 'pool not available' in run(server._pg_query('SELECT 1'))


def test_head_rejects_write_before_connect(server):
    only(server, '_SELECT_RE')
    server._pool = MagicMock()
    assert 'Only SELECT' in run(server._pg_query('DELETE FROM locations'))
    server._pool.acquire.assert_not_called()


@pytest.mark.parametrize('login', ['owntracks_ro', 'owntracks_rw', 'owntracks_adm'])
def test_selected_identity_and_command_once(server, login):
    only(server, 'PG_LOGINS')
    conn = MagicMock()
    statement = MagicMock()
    statement.get_attributes.return_value = ()
    statement.fetch = AsyncMock()
    conn.prepare = AsyncMock(return_value=statement)
    conn.execute = AsyncMock(return_value='UPDATE 1')
    pool = pool_with(conn)
    server._pools = {login: pool}
    assert json.loads(run(server._pg_query('UPDATE example SET n=1', login))) == {'status': 'UPDATE 1'}
    conn.execute.assert_awaited_once_with('UPDATE example SET n=1', timeout=30)
    statement.fetch.assert_not_awaited()


def test_unknown_identity(server):
    only(server, 'PG_LOGINS')
    assert 'Unknown database login' in run(server._pg_query('SELECT 1', 'postgres'))


def test_database_error_not_retried(server):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=RuntimeError('permission denied'))
    statement = MagicMock()
    statement.get_attributes.return_value = ('value',)
    statement.fetch = AsyncMock(side_effect=RuntimeError('permission denied'))
    conn.prepare = AsyncMock(return_value=statement)
    conn.execute = AsyncMock()
    pool = pool_with(conn)
    if server.variant == 'head':
        server._pool = pool
    else:
        server._pools = {'owntracks_ro': pool}
    assert 'permission denied' in run(server._pg_query('SELECT 1'))
    (conn.fetch if server.variant == 'head' else statement.fetch).assert_awaited_once()
    conn.execute.assert_not_awaited()


@pytest.mark.parametrize('target', ['db_size', 'connections', 'long_queries', 'bloat',
                                  'xid', 'locks', 'cache', 'freshness', 'instance'])
def test_monitoring_query_targets(server, monkeypatch, target):
    only(server, '_run_check')
    rows = {
        'db_size': [{'database': 'owntracks', 'bytes': 100, 'size': '100 bytes'}],
        'connections': [{'max_connections': 100, 'current': 95, 'pct': 95}],
        'long_queries': [{'running_secs': 301}],
        'bloat': [{'dead_pct': 50}], 'xid': [{'pct_to_wraparound': 85}],
        'locks': [{'waiting_locks': 1, 'sessions_waiting': 0}],
        'cache': [{'cache_hit_pct': 80}], 'freshness': [{'latest_fix_utc': None}],
        'instance': [{'version': '15', 'uptime_secs': 30}],
    }
    fetch = AsyncMock(return_value=rows[target])
    monkeypatch.setattr(server, '_fetchrows', fetch)
    result = json.loads(run(server._run_check(target)))
    expected = {'connections': 'crit', 'long_queries': 'crit', 'bloat': 'crit',
                'xid': 'crit', 'locks': 'warn', 'cache': 'warn'}.get(target, 'info')
    assert result['status'] == expected
    fetch.assert_awaited_once()


@pytest.mark.parametrize('free,status', [(25, 'ok'), (15, 'warn'), (5, 'crit')])
def test_disk_thresholds(server, monkeypatch, free, status):
    only(server, '_check_disk')
    server._DISK_PATHS = ['/fake']
    server._DISK_WARN_PCT = 20
    server._DISK_CRIT_PCT = 10
    monkeypatch.setattr(server.os, 'statvfs', lambda p: types.SimpleNamespace(
        f_blocks=100, f_frsize=10000000, f_bavail=free))
    result = run(server._check_disk())
    assert result['status'] == status
    assert result['filesystems'][0]['free_pct'] == free


def test_monitoring_rollup_failure_and_unknown(server):
    only(server, '_run_check')
    server._CHECKS = {'healthy': AsyncMock(return_value={'status': 'ok'}),
                      'broken': AsyncMock(side_effect=RuntimeError('fixture failure'))}
    result = json.loads(run(server._run_check('all')))
    assert result['overall_status'] == 'warn'
    assert result['checks']['broken']['error'] == 'fixture failure'
    assert 'unknown target' in json.loads(run(server._run_check('missing')))['error']


def test_webdav_dispatch(server, monkeypatch):
    only(server, 'misc_webdav')
    handler = AsyncMock(return_value='fixture response')
    monkeypatch.setattr(server.misc_webdav, 'call', handler)
    result = run(server._make_tool_server().call('webdav', {'method': 'GET', 'path': '/fixture'}))
    assert result[0].text == 'fixture response'
    handler.assert_awaited_once_with({'method': 'GET', 'path': '/fixture'})


def mailbox(server, message):
    client = MagicMock()
    server.IMAPClient.return_value.__enter__.return_value = client
    client.search.return_value = [b'1', b'2', b'3']
    client.connection.fetch.return_value = ('OK', [(b'1 (FLAGS (\\Seen))', message)])
    client.list_folders.return_value = ['INBOX', 'Archive']
    return client


def test_email_list_headers_and_limit(server):
    only(server, '_email_list')
    client = mailbox(server, b'From: sender@example.test\r\nSubject: Fixture\r\n\r\n')
    result = json.loads(run(server._email_list('Archive', 'UNSEEN', 2)))
    assert result['count'] == 2
    assert [x['subject'] for x in result['emails']] == ['Fixture', 'Fixture']
    client.select_folder.assert_called_once_with('Archive', readonly=True)
    client.search.assert_called_once_with('UNSEEN')
    assert all(call.args[1] == '(UID FLAGS BODY.PEEK[HEADER])' for call in client.connection.fetch.call_args_list)


@pytest.mark.parametrize('message', [b'Subject: Fixture\r\n\r\nHello',
    b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\nContent-Type: text/plain\r\n\r\nHello\r\n--x--\r\n'])
def test_email_read_plain_and_multipart(server, message):
    only(server, '_email_read')
    client = mailbox(server, message)
    result = json.loads(run(server._email_read('INBOX', '1')))
    assert result['body'].strip() == 'Hello'
    client.connection.fetch.assert_called_once_with('1', '(FLAGS BODY.PEEK[])')


def test_email_missing_and_fetch_failure(server):
    only(server, '_email_read')
    client = mailbox(server, b'')
    assert json.loads(run(server._email_read()))['status'] == 'error'
    client.connection.fetch.return_value = ('NO', [])
    assert 'not found' in json.loads(run(server._email_read(seq_id='9')))['error']


def test_email_folders_and_connection_error(server):
    only(server, '_email_list_folders')
    mailbox(server, b'')
    assert json.loads(run(server._email_list_folders()))['folders'] == ['INBOX', 'Archive']
    server.IMAPClient.side_effect = RuntimeError('fixture connection failure')
    for coro in [server._email_list(), server._email_read(seq_id='1'), server._email_list_folders()]:
        assert json.loads(run(coro))['status'] == 'error'


def test_email_send_delivery(server, monkeypatch):
    only(server, '_email_send')
    import netrc
    from email.parser import BytesParser
    webdav = types.ModuleType('webdav4')
    clientmod = types.ModuleType('webdav4.client')
    clientmod.Client = MagicMock()
    webdav.client = clientmod
    monkeypatch.setitem(sys.modules, 'webdav4', webdav)
    monkeypatch.setitem(sys.modules, 'webdav4.client', clientmod)
    credentials = MagicMock()
    credentials.authenticators.return_value = ('fixture-user', None, 'fixture-password')
    monkeypatch.setattr(netrc, 'netrc', lambda: credentials)
    result = json.loads(run(server._email_send('recipient@example.test', 'Fixture', 'Hello', {'X-Test': 'yes'})))
    assert result['status'] == 'sent'
    upload = clientmod.Client.return_value.upload_fileobj.call_args
    message = BytesParser().parsebytes(upload.args[0].getvalue())
    assert message['To'] == 'recipient@example.test'
    assert message['X-Test'] == 'yes'
    assert 'Hello' in message.get_payload()
    assert upload.args[1].startswith('new/E-')
    clientmod.Client.return_value.upload_fileobj.side_effect = RuntimeError('fixture upload failure')
    assert json.loads(run(server._email_send('x@example.test', 'Fixture', 'Hello')))['status'] == 'error'


def test_plugin_registration_and_dispatch(server, monkeypatch):
    only(server, 'discover_tools')
    plugin = types.SimpleNamespace(name='fixture_plugin', description='Fixture',
                                   input_schema={'type': 'object'}, invoke=AsyncMock(return_value='plugin result'))
    monkeypatch.setattr(server, 'discover_tools', lambda: [plugin])
    tools = server._make_tool_server()
    assert 'fixture_plugin' in {t.name for t in run(tools.list())}
    assert run(tools.call('fixture_plugin', {'n': 1}))[0].text == 'plugin result'
    plugin.invoke.assert_awaited_once_with({'n': 1})


@pytest.mark.parametrize('sse,streamable,paths', [('true', 'false', ['/mcp/', '/mcp/messages']),
    ('false', 'true', ['/mcp']), ('true', 'true', ['/mcp/', '/mcp/messages', '/mcp'])])
def test_transport_routes(server, monkeypatch, sse, streamable, paths):
    monkeypatch.setenv('MCP_SSE', sse)
    monkeypatch.setenv('MCP_STREAMABLE', streamable)
    monkeypatch.setattr(server, '_make_tool_server', lambda name: MagicMock())
    monkeypatch.setattr(server, 'StreamableHTTPSessionManager', MagicMock())
    assert [r.path for r in server.make_mcp_app().routes] == paths


def test_transport_cannot_disable_both(server, monkeypatch):
    monkeypatch.setenv('MCP_SSE', 'false')
    monkeypatch.setenv('MCP_STREAMABLE', 'false')
    with pytest.raises(RuntimeError, match='At least one'):
        server.make_mcp_app()


def test_rest_store_header(server):
    import httpx
    async def request():
        app = server.make_rest_app(store_name='fixture-store')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
            response = await client.get('/missing')
        assert response.headers['x-mcp-store'] == 'fixture-store'
    run(request())


def test_startup_pool_cleanup_and_binding(server, monkeypatch):
    pools = []
    configs = []
    def create_pool(*args, **kwargs):
        pool = MagicMock()
        pool.close = AsyncMock()
        pool.__aenter__ = AsyncMock(return_value=pool)
        pool.__aexit__ = AsyncMock(return_value=False)
        pools.append((pool, args, kwargs))
        if server.variant == 'head':
            async def ready():
                return pool
            return ready()
        return pool
    monkeypatch.setattr(server.asyncpg, 'create_pool', create_pool)
    monkeypatch.setattr(server, 'make_rest_app', lambda *a: 'rest')
    monkeypatch.setattr(server, 'make_mcp_app', lambda *a: 'mcp')
    def config(app, **kwargs):
        configs.append((app, kwargs))
        return kwargs
    monkeypatch.setattr(server.uvicorn, 'Config', config)
    fake_uvicorn = MagicMock()
    fake_uvicorn.serve = AsyncMock()
    monkeypatch.setattr(server.uvicorn, 'Server', lambda config: fake_uvicorn)
    monkeypatch.setattr(sys, 'argv', ['misc', '--rest-port', '9010', '--mcp-port', '9011'])
    for key in ['MISC_HOST', 'REST_HOST', 'MCP_HOST']:
        monkeypatch.delenv(key, raising=False)
    run(server.main())
    assert configs[0][1]['port'] == 9010
    assert configs[1][1]['port'] == 9011
    assert all(c[1]['host'] == '127.0.0.1' for c in configs)
    assert fake_uvicorn.serve.await_count == 2
    if server.variant == 'head':
        assert len(pools) == 1
        pools[0][0].close.assert_awaited_once()
    else:
        assert [p[2]['user'] for p in pools] == list(server.PG_LOGINS)
        assert not server._pools
        assert all(p[0].__aexit__.await_count == 1 for p in pools)


@pytest.mark.parametrize('tool,args,handler,expected_args', [
    ('email_list', {}, '_email_list', ('INBOX', 'ALL', 10)),
    ('email_read', {'seq_id': '2'}, '_email_read', ('INBOX', '2')),
    ('email_list_folders', {}, '_email_list_folders', ()),
    ('email_send', {'to': 'x@example.test', 'subject': 'Fixture', 'body': 'Hello'},
     '_email_send', ('x@example.test', 'Fixture', 'Hello', None)),
])
def test_mail_dispatch(server, monkeypatch, tool, args, handler, expected_args):
    only(server, handler)
    stub = AsyncMock(return_value='fixture result')
    monkeypatch.setattr(server, handler, stub)
    assert run(server._make_tool_server().call(tool, args))[0].text == 'fixture result'
    stub.assert_awaited_once_with(*expected_args)


@pytest.mark.parametrize('target,rows,status', [
    ('connections', [{'pct': 40}], 'ok'), ('connections', [{'pct': 85}], 'warn'),
    ('long_queries', [], 'ok'), ('long_queries', [{'running_secs': 60}], 'warn'),
    ('bloat', [], 'ok'), ('bloat', [{'dead_pct': 25}], 'warn'),
    ('xid', [{'pct_to_wraparound': 10}], 'ok'), ('xid', [{'pct_to_wraparound': 55}], 'warn'),
    ('locks', [{'waiting_locks': 0, 'sessions_waiting': 0}], 'ok'),
    ('cache', [{'cache_hit_pct': 99}], 'ok'),
])
def test_monitoring_healthy_and_warning(server, monkeypatch, target, rows, status):
    only(server, '_run_check')
    monkeypatch.setattr(server, '_fetchrows', AsyncMock(return_value=rows))
    assert json.loads(run(server._run_check(target)))['status'] == status


def test_monitoring_read_only_pool(server):
    only(server, '_fetchrows')
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{'n': 1}])
    if hasattr(server, '_pools'):
        server._pools = {'owntracks_ro': pool_with(conn)}
    else:
        server._pool = pool_with(conn)
    assert run(server._fetchrows('SELECT 1 AS n')) == [{'n': 1}]
    conn.fetch.assert_awaited_once_with('SELECT 1 AS n', timeout=30)


def test_disk_error_reported(server, monkeypatch):
    only(server, '_check_disk')
    monkeypatch.setattr(server.os, 'statvfs', MagicMock(side_effect=OSError('unavailable')))
    result = run(server._check_disk())
    assert result['status'] == 'warn'
    assert all('error' in entry for entry in result['filesystems'])
