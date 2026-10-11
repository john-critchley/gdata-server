import asyncio
import base64
import json

import httpx
import pytest

import misc_webdav as dav


@pytest.fixture
def credentials(tmp_path):
    path = tmp_path / 'netrc'
    path.write_text('machine webdav.critchley.biz login test password test-secret\n')
    path.chmod(0o600)
    return str(path)


def run(arguments, credentials, handler):
    return asyncio.run(dav.request(arguments, netrc_file=credentials,
                                  transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize('method', dav.METHODS)
def test_methods_and_auth(method, credentials):
    arguments = {'method': method, 'path': '/test/a file.txt'}
    if method in ('COPY', 'MOVE'):
        arguments['destination'] = '/test/new file.txt'

    def handler(req):
        assert req.method == method
        assert str(req.url) == 'https://webdav.critchley.biz/test/a%20file.txt'
        assert req.headers['authorization'] == 'Basic ' + base64.b64encode(b'test:test-secret').decode()
        if method in ('COPY', 'MOVE'):
            assert req.headers['destination'] == 'https://webdav.critchley.biz/test/new%20file.txt'
            assert req.headers['overwrite'] == 'F'
        if method == 'PROPFIND':
            assert req.headers['depth'] == '1'
            assert b'allprop' in req.content
        return httpx.Response(207 if method == 'PROPFIND' else 200, content=b'<result/>')

    result = run(arguments, credentials, handler)
    assert result['ok']
    assert result['body'] == '<result/>'


@pytest.mark.parametrize('path', ['https://evil.example/x', '//evil.example/x',
                                  'http://webdav.critchley.biz/x', '/a/../b',
                                  'https://webdav.critchley.biz/a/%2e%2e/b',
                                  'https://user@webdav.critchley.biz/x', '/a\\b', '/x\n'])
def test_reject_other_hosts_and_ambiguous_paths(path):
    with pytest.raises(ValueError):
        dav._url(path)


def test_full_url_and_literal_percent():
    assert dav._url('https://webdav.critchley.biz/a%20b') == dav._url('/a b')
    assert dav._url('/a%20b').endswith('/a%2520b')


def test_binary_roundtrip(credentials):
    data = b'\x00\xff\xfe'

    def handler(req):
        assert req.content == data
        return httpx.Response(200, content=data, headers={'ETag': 'v1'})

    result = run({'method': 'PUT', 'path': '/test',
                  'body_base64': base64.b64encode(data).decode()}, credentials, handler)
    assert result['encoding'] == 'base64'
    assert base64.b64decode(result['body']) == data
    assert result['headers']['etag'] == 'v1'


def test_no_redirect_follow_or_cookie_exposure(credentials):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(302, headers={'Location': 'https://evil.example/',
                                           'Set-Cookie': 'secret=value'})

    result = run({'method': 'GET', 'path': '/'}, credentials, handler)
    assert len(calls) == 1
    assert result['status'] == 302
    assert not result['ok']
    assert 'set-cookie' not in result['headers']


@pytest.mark.parametrize('extra', [
    {'headers': {'Authorization': 'secret'}},
    {'headers': {'Host': 'evil.example'}},
    {'headers': {'If': 'bad\r\nvalue'}},
    {'body': 'text', 'body_base64': 'AA=='},
    {'body_base64': 'invalid!'},
    {'response_encoding': 'wrong'},
    {'destination': '/unneeded'},
])
def test_invalid_input_never_sent(extra, credentials):
    def handler(req):
        pytest.fail('Invalid input reached the server')
    with pytest.raises(ValueError):
        run({'method': 'GET', 'path': '/', **extra}, credentials, handler)


def test_no_default_credentials(tmp_path):
    path = tmp_path / 'netrc'
    path.write_text('default login someone password secret\n')
    path.chmod(0o600)
    with pytest.raises(ValueError, match='Missing'):
        run({'method': 'GET', 'path': '/'}, str(path), lambda req: pytest.fail('sent'))


def test_response_limit(credentials, monkeypatch):
    monkeypatch.setattr(dav, 'MAX_BYTES', 4)
    with pytest.raises(ValueError, match='Response exceeds'):
        run({'method': 'GET', 'path': '/'}, credentials,
            lambda req: httpx.Response(200, content=b'12345'))


def test_write_failure_preserves_status(credentials):
    result = run({'method': 'PUT', 'path': '/test', 'body': 'x',
                  'headers': {'If-Match': 'old'}}, credentials,
                 lambda req: httpx.Response(412, content=b'Precondition failed'))
    assert not result['ok']
    assert result['status'] == 412


def test_credential_parse_error_is_redacted(monkeypatch, caplog):
    async def fail(arguments):
        raise dav.netrc.NetrcParseError('a-secret-value')
    monkeypatch.setattr(dav, 'request', fail)
    result = json.loads(asyncio.run(dav.call({'method': 'GET', 'path': '/'})))
    assert not result['ok']
    assert 'a-secret-value' not in json.dumps(result) + caplog.text
