"""Regression checks using real Misc, IMAP and plugin modules; no live services."""
import asyncio
import json
from email.message import EmailMessage
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
import imap_client
import mcp_tool_plugins as plugins
import misc_mcp_server as server


def run(coro):
    return asyncio.run(coro)


def test_real_dependencies_and_tool_union():
    from mcp.types import ListToolsRequest
    # Inspect the real server's registered request handlers.
    toolserver = server._make_tool_server()
    result = run(toolserver.request_handlers[ListToolsRequest](ListToolsRequest(method='tools/list')))
    assert {'hello', 'pg_query', 'check', 'webdav', 'email_send', 'email_list',
            'email_read', 'email_list_folders'} <= {tool.name for tool in result.root.tools}


@pytest.mark.parametrize('limit', [0, -1, 501, True, '10'])
def test_invalid_mail_limits_do_not_connect(monkeypatch, limit):
    client = MagicMock()
    monkeypatch.setattr(server, 'IMAPClient', client)
    assert json.loads(run(server._email_list(max_results=limit)))['status'] == 'error'
    client.assert_not_called()


def test_nested_charset_body_and_worker_thread(monkeypatch):
    import threading
    message = EmailMessage()
    message.set_content('caf\u00e9', charset='iso-8859-1')
    message.add_alternative('<p>caf\u00e9</p>', subtype='html')
    outer = EmailMessage()
    outer.make_mixed()
    outer.attach(message)
    client = MagicMock()
    client.connection.fetch.return_value = ('OK', [(b'1 (FLAGS ())', outer.as_bytes())])
    thread_ids = []
    factory = MagicMock()
    def enter():
        thread_ids.append(threading.get_ident())
        return client
    factory.return_value.__enter__.side_effect = enter
    monkeypatch.setattr(server, 'IMAPClient', factory)
    result = json.loads(run(server._email_read(seq_id='1')))
    assert result['body'].strip() == 'caf\u00e9'
    assert thread_ids[0] != threading.get_ident()


def test_imap_ssl_timeout_and_read_only(monkeypatch):
    credentials = MagicMock()
    credentials.authenticators.return_value = ('fixture', None, 'fixture-password')
    monkeypatch.setattr(imap_client.netrc, 'netrc', lambda: credentials)
    connection = MagicMock()
    connection.select.return_value = ('OK', [b'3'])
    factory = MagicMock(return_value=connection)
    monkeypatch.setattr(imap_client.imaplib, 'IMAP4_SSL', factory)
    client = imap_client.IMAPClient('fixture.example')
    factory.assert_called_once_with('fixture.example', timeout=30)
    assert client.select_folder('INBOX', readonly=True) == ('OK', 3)
    connection.select.assert_called_once_with('INBOX', readonly=True)
    client.close()
    connection.logout.assert_called_once()


def test_plugin_load_reload_and_failure(tmp_path):
    path = tmp_path / 'fixture_tools.py'
    source = '''from mcp_tool_plugins import mcp_tool
@mcp_tool('fixture', 'Fixture', {'type': 'object'})
def handler():
    return {'version': VERSION}
'''
    path.write_text(source.replace('VERSION', '1'))
    assert json.loads(run(plugins.discover_tools([str(tmp_path)])[0].invoke({}))) == {'version': 1}
    path.write_text('def broken(:\n')
    assert json.loads(run(plugins.discover_tools([str(tmp_path)])[0].invoke({}))) == {'version': 1}
    path.write_text(source.replace('VERSION', '2'))
    assert json.loads(run(plugins.discover_tools([str(tmp_path)])[0].invoke({}))) == {'version': 2}


def test_plugin_async_failure_and_test_exclusion(tmp_path):
    (tmp_path / 'test_bad_tools.py').write_text('raise RuntimeError("must not load")')
    (tmp_path / 'fixture_tools.py').write_text('''from mcp_tool_plugins import mcp_tool
@mcp_tool('fixture', 'Fixture', {'type': 'object'})
async def handler():
    raise ValueError('fixture failure')
''')
    discovered = plugins.discover_tools([str(tmp_path)])
    assert len(discovered) == 1
    assert json.loads(run(discovered[0].invoke({})))['exception_type'] == 'ValueError'


@pytest.mark.parametrize('argv,env,expected', [
    (['--host', '192.0.2.1'], {}, ('192.0.2.1', '192.0.2.1')),
    ([], {'MISC_HOST': '192.0.2.2'}, ('192.0.2.2', '192.0.2.2')),
    (['--rest-host', '192.0.2.3'], {'MCP_HOST': '192.0.2.4'}, ('192.0.2.3', '192.0.2.4')),
])
def test_binding_and_optional_pool_failure(monkeypatch, argv, env, expected):
    for key in ['MISC_HOST', 'REST_HOST', 'MCP_HOST']:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, 'argv', ['misc', *argv])
    monkeypatch.setattr(server.gdata_oauth, 'startup_check', lambda: None)
    pool = MagicMock()
    pool.__aenter__ = AsyncMock(return_value=pool)
    pool.__aexit__ = AsyncMock(return_value=False)
    def create_pool(**kwargs):
        if kwargs['user'] != 'owntracks_ro':
            raise RuntimeError('fixture role unavailable')
        return pool
    monkeypatch.setattr(server.asyncpg, 'create_pool', create_pool)
    monkeypatch.setattr(server, 'make_rest_app', lambda *args: None)
    monkeypatch.setattr(server, 'make_mcp_app', lambda *args: None)
    hosts = []
    monkeypatch.setattr(server.uvicorn, 'Config', lambda app, **kwargs: hosts.append(kwargs['host']))
    fake = MagicMock()
    fake.serve = AsyncMock()
    monkeypatch.setattr(server.uvicorn, 'Server', lambda config: fake)
    run(server.main())
    assert tuple(hosts) == expected
    assert not server._pools
    pool.__aexit__.assert_awaited_once()


def test_stable_mail_uid(monkeypatch):
    client = MagicMock()
    client.search.return_value = [b'2']
    client.connection.fetch.return_value = ('OK', [(b'2 (UID 42 FLAGS ())', b'Subject: Fixture\r\n\r\n')])
    client.connection.uid.return_value = ('OK', [(b'2 (UID 42 FLAGS ())', b'Subject: Fixture\r\n\r\nHello')])
    factory = MagicMock()
    factory.return_value.__enter__.return_value = client
    monkeypatch.setattr(server, 'IMAPClient', factory)
    assert json.loads(run(server._email_list()))['emails'][0]['uid'] == '42'
    assert json.loads(run(server._email_read(uid='42')))['body'].strip() == 'Hello'
    client.connection.uid.assert_called_once_with('FETCH', '42', '(FLAGS BODY.PEEK[])')
