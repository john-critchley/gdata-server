"""WebDAV tool implementation. Usage notes: misc-server/webdav."""

import base64
import json
import logging
import netrc
import traceback
from urllib.parse import quote, unquote, urlsplit

import httpx

logger = logging.getLogger(__name__)
ORIGIN = 'https://webdav.critchley.biz'
METHODS = ['GET', 'HEAD', 'OPTIONS', 'PUT', 'DELETE', 'PROPFIND',
           'PROPPATCH', 'MKCOL', 'COPY', 'MOVE', 'LOCK', 'UNLOCK']
HEADERS = {'content-type', 'depth', 'overwrite', 'if', 'if-match',
           'if-none-match', 'lock-token', 'timeout', 'range'}
MAX_BYTES = 8 * 1024 * 1024
TOOL_SCHEMA = {
    'type': 'object',
    'properties': {
        'method': {'type': 'string', 'enum': METHODS},
        'path': {'type': 'string', 'description': 'Unescaped server path or same-host HTTPS URL.'},
        'destination': {'type': 'string', 'description': 'Destination path for COPY/MOVE.'},
        'body': {'type': 'string', 'description': 'UTF-8 file content or WebDAV XML.'},
        'body_base64': {'type': 'string', 'description': 'Binary content; exclusive with body.'},
        'headers': {'type': 'object', 'additionalProperties': {'type': 'string'},
                    'description': 'Content-Type, Depth, Overwrite, If, If-Match, If-None-Match, Lock-Token, Timeout, Range.'},
        'response_encoding': {'type': 'string', 'enum': ['text', 'base64'], 'default': 'text'},
    },
    'required': ['method', 'path'],
    'additionalProperties': False,
}


def _url(path: str) -> str:
    if not isinstance(path, str) or not path or any(ord(c) < 32 for c in path):
        raise ValueError('A non-empty path without control characters is required.')
    if path.startswith('https://'):
        parsed = urlsplit(path)
        if (parsed.netloc != 'webdav.critchley.biz' or parsed.query or parsed.fragment):
            raise ValueError('URLs must use https://webdav.critchley.biz without query or fragment.')
        path = unquote(parsed.path)
    elif '://' in path or path.startswith('//'):
        raise ValueError('Only server paths or same-host HTTPS URLs are permitted.')
    if '\\' in path or any(p in ('.', '..') for p in path.split('/')):
        raise ValueError('Backslashes and dot path segments are not permitted.')
    return ORIGIN + '/' + quote(path.lstrip('/'), safe='/')


async def request(arguments: dict, *, transport=None, netrc_file=None) -> dict:
    """Issue one authenticated request; transport/netrc_file support isolated tests."""
    method = arguments['method']
    if method not in METHODS:
        raise ValueError('Unsupported WebDAV method.')
    url = _url(arguments['path'])
    headers = arguments.get('headers', {})
    if not isinstance(headers, dict) or any(
            not isinstance(k, str) or k.lower() not in HEADERS or not isinstance(v, str)
            or '\r' in v or '\n' in v for k, v in headers.items()):
        raise ValueError('Unsupported or invalid request header.')
    headers = {k.lower(): v for k, v in headers.items()}
    if method in ('COPY', 'MOVE'):
        headers['destination'] = _url(arguments['destination'])
        headers.setdefault('overwrite', 'F')
    elif 'destination' in arguments:
        raise ValueError('destination applies only to COPY and MOVE.')
    if 'body' in arguments and 'body_base64' in arguments:
        raise ValueError('Supply body or body_base64, not both.')
    body = (base64.b64decode(arguments['body_base64'], validate=True)
            if 'body_base64' in arguments else arguments.get('body', '').encode('utf-8'))
    if len(body) > MAX_BYTES:
        raise ValueError('Request body exceeds the 8 MiB limit.')
    encoding = arguments.get('response_encoding', 'text')
    if encoding not in ('text', 'base64'):
        raise ValueError('response_encoding must be text or base64.')
    if method == 'PROPFIND':
        headers.setdefault('depth', '1')
        if not body:
            body = b'<d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'
    if method in ('PROPFIND', 'PROPPATCH', 'LOCK'):
        headers.setdefault('content-type', 'application/xml; charset=utf-8')
    # Read explicitly rather than accepting a default netrc entry for this host.
    auth = netrc.netrc(netrc_file).hosts.get('webdav.critchley.biz')
    if not auth or not auth[0] or not auth[2]:
        raise ValueError('Missing webdav.critchley.biz login/password in the server user\'s .netrc.')
    async with httpx.AsyncClient(auth=(auth[0], auth[2]), timeout=30,
                                 follow_redirects=False, transport=transport) as client:
        async with client.stream(method, url, headers=headers, content=body) as response:
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_BYTES:
                    raise ValueError('Response exceeds 8 MiB; use a Range header for GET.')
            data = bytes(chunks)
            result = {
                'status': response.status_code,
                'ok': 200 <= response.status_code < 300,
                'headers': {k: v for k, v in response.headers.items()
                            if k in {'content-type', 'content-length', 'etag', 'last-modified',
                                     'location', 'lock-token', 'dav', 'allow', 'content-range'}},
                'encoding': encoding,
            }
            if encoding == 'base64':
                result['body'] = base64.b64encode(data).decode('ascii')
            else:
                try:
                    result['body'] = data.decode('utf-8')
                except UnicodeDecodeError:
                    result['encoding'] = 'base64'
                    result['body'] = base64.b64encode(data).decode('ascii')
            return result


async def call(arguments: dict) -> str:
    try:
        return json.dumps(await request(arguments))
    except (ValueError, KeyError, TypeError, AttributeError, OSError,
            netrc.NetrcParseError, httpx.HTTPError) as exc:
        # Do not log exception text: malformed netrc errors can contain secrets.
        frames = traceback.extract_tb(exc.__traceback__)
        logger.error('WebDAV request failed: %s (method=%r)\n%s',
                     type(exc).__name__, arguments.get('method') if arguments.get('method') in METHODS else None,
                     '\n'.join(f'{f.filename}:{f.lineno} in {f.name}' for f in frames))
        return json.dumps({'ok': False, 'error': type(exc).__name__,
                           'message': 'WebDAV request failed. Check arguments, server .netrc and connectivity. '
                                      'For a write with uncertain outcome, inspect state before retrying.'})
